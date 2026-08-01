"""Generic stream consumer — Redis consumer group in, pluggable apply out.

At-least-once by construction: entries are acknowledged only AFTER the
apply function returns. On start, the consumer drains its own pending
entries (delivered before a crash, never acked) before asking for new
ones — resuming is the same code path as running.

Poison entries (payloads that fail message validation) are counted,
logged, and acked: an unparseable entry would otherwise be redelivered
forever and wedge the stream. Apply failures are contained rather than
fatal: retry with backoff, then binary-split to isolate the poisonous
entry into the dead-letter stream (D14 addendum, #44).

TODO(#32): single consumer per group today; concurrent-consumer safety
is untested. Validate before trusting the scale-out lever.
"""

import logging
import time
from collections.abc import Callable

from pydantic import ValidationError

from resgraph import obs
from resgraph.schema import UpdateMessage

log = logging.getLogger("resgraph.consumer")

DEFAULT_STREAM = "resgraph:updates"

# Receives one parsed batch; returns (applied, skipped).
ApplyFn = Callable[[list[UpdateMessage]], tuple[int, int]]


class StreamConsumer:
    def __init__(
        self,
        redis_url: str,
        apply_fn: ApplyFn,
        stream: str = DEFAULT_STREAM,
        group: str = "resgraph-ingest",
        name: str = "c1",
        # 1024 measured as the hot-path throughput sweet spot; 2048
        # regresses (BENCHMARKS.md, ingest section)
        batch: int = 1024,
        block_ms: int = 1000,
        max_deliveries: int = 5,
        max_retries: int = 3,
        backoff_s: float = 0.5,
        retryable_exceptions: tuple[type[BaseException], ...] = (),
        outage_backoff_cap_s: float = 5.0,
    ) -> None:
        import redis

        self.r = redis.Redis.from_url(redis_url)
        self.apply_fn = apply_fn
        self.stream = stream
        self.group = group
        self.name = name
        self.batch = batch
        self.block_ms = block_ms
        self.max_deliveries = max_deliveries
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.retryable_exceptions = retryable_exceptions
        self.outage_backoff_cap_s = outage_backoff_cap_s
        self.dlq = f"{stream}:dlq"
        self._attrs = {"worker": name}
        obs.register_lag_reader(name, self._read_lag)

    def _read_lag(self) -> int | None:
        try:
            for g in self.r.xinfo_groups(self.stream):
                gname = g.get("name")
                if gname in (self.group, self.group.encode()):
                    return g.get("lag")
        except Exception:
            return None
        return None

    def ensure_group(self) -> None:
        """Create the consumer group at the stream's beginning (idempotent).

        id='0' so a group created after the producer started still sees
        every entry — idempotent applies make overlap harmless, so
        erring toward re-reading is free.
        """
        import redis

        try:
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def run(self, max_messages: int | None = None, exit_on_idle: bool = False) -> dict[str, int]:
        """Consume until stopped. `applied` vs `skipped` is the apply
        layer's scoreboard — the first thing to look at when deliveries
        were retried."""
        self.ensure_group()
        counters = {"read": 0, "applied": 0, "skipped": 0, "invalid": 0, "dead_lettered": 0}
        cursor = "0"  # own pending first (crash recovery), then new entries
        try:
            while True:
                if max_messages is not None and counters["read"] >= max_messages:
                    break
                count = self.batch
                if max_messages is not None:
                    count = min(count, max_messages - counters["read"])
                resp = self.r.xreadgroup(
                    self.group,
                    self.name,
                    {self.stream: cursor},
                    count=count,
                    block=self.block_ms if cursor == ">" else None,
                )
                entries = resp[0][1] if resp else []
                if not entries:
                    if cursor != ">":
                        cursor = ">"  # pending drained; switch to new entries
                        continue
                    if exit_on_idle:
                        break
                    continue
                self._apply_batch(entries, counters, pending=cursor == "0")
        except KeyboardInterrupt:
            pass  # an interrupt is a stop request; the counters still count
        return counters

    def _apply_batch(self, entries, counters: dict[str, int], pending: bool = False) -> None:
        before = dict(counters)
        t0 = time.perf_counter()
        obs.READ.add(len(entries), self._attrs)
        pairs = []  # (entry_id, raw payload, parsed message)
        invalid = []
        for entry_id, fields in entries:
            counters["read"] += 1
            try:
                raw = fields[b"data"]
                pairs.append((entry_id, raw, UpdateMessage.model_validate_json(raw)))
            except (ValidationError, KeyError) as e:
                counters["invalid"] += 1
                log.warning("poison entry %s acked and dropped: %s", entry_id, e)
                invalid.append(entry_id)
        if invalid:
            obs.INVALID.add(len(invalid), self._attrs)
            self.r.xack(self.stream, self.group, *invalid)
        if pending and pairs:
            # In-process retries never bump the delivery counter, so the
            # cap only fires here: an entry still arriving in the pending
            # drain after repeated crashes is quarantined, not retried.
            pairs = self._quarantine_over_delivered(pairs, counters)
        self._apply_entries(pairs, counters, self.max_retries)
        obs.get_sink(f"consumer-{self.name}").emit(
            "consume_batch",
            worker=self.name,
            stream=self.stream,
            entries=len(entries),
            ms=round((time.perf_counter() - t0) * 1e3, 2),
            **{k: counters[k] - before[k] for k in counters},
        )

    def _apply_entries(self, pairs, counters: dict[str, int], retries: int) -> None:
        if not pairs:
            return
        msgs = [m for _, _, m in pairs]
        err = None
        attempt = 0
        outage_attempts = 0
        while True:
            try:
                t0 = time.perf_counter()
                applied, skipped = self.apply_fn(msgs)
                obs.BATCH_SECONDS.record(time.perf_counter() - t0, self._attrs)
            except self.retryable_exceptions as e:
                # Connection-class failure: the store is down, the
                # messages are fine. Wait for it to come back — never
                # counts as poison, never splits, never dead-letters
                # (D14 supersession: outage is not poison).
                outage_attempts += 1
                if outage_attempts == 1 or outage_attempts % 20 == 0:
                    log.warning(
                        "store unavailable, holding %d entries (attempt %d): %s",
                        len(msgs),
                        outage_attempts,
                        e,
                    )
                delay = self.backoff_s * 2 ** min(outage_attempts, 6)
                time.sleep(min(delay, self.outage_backoff_cap_s))
                continue
            except Exception as e:
                err = e
                log.warning(
                    "apply failed for %d entries (attempt %d/%d): %s",
                    len(msgs),
                    attempt + 1,
                    retries + 1,
                    e,
                )
                attempt += 1
                if attempt > retries:
                    break
                time.sleep(self.backoff_s * 2 ** (attempt - 1))
                continue
            counters["applied"] += applied
            counters["skipped"] += skipped
            obs.APPLIED.add(applied, self._attrs)
            obs.SKIPPED.add(skipped, self._attrs)
            # ack strictly after the apply committed: crash before this line
            # redelivers the batch, and idempotent apply absorbs it.
            self.r.xack(self.stream, self.group, *[eid for eid, _, _ in pairs])
            return
        if len(pairs) == 1:
            entry_id, raw, _ = pairs[0]
            self._dead_letter(entry_id, raw, err, counters)
            return
        # The full batch survived the backoff ladder, so the failure is
        # treated as deterministic: halves get one attempt each (D14
        # addendum — halving isolates one poison in ~log2(batch) applies).
        mid = len(pairs) // 2
        self._apply_entries(pairs[:mid], counters, retries=0)
        self._apply_entries(pairs[mid:], counters, retries=0)

    def _quarantine_over_delivered(self, pairs, counters: dict[str, int]):
        info = self.r.xpending_range(
            self.stream,
            self.group,
            min=pairs[0][0],
            max=pairs[-1][0],
            count=len(pairs),
            consumername=self.name,
        )
        deliveries = {e["message_id"]: e["times_delivered"] for e in info}
        kept = []
        for entry_id, raw, msg in pairs:
            n = deliveries.get(entry_id, 1)
            if n > self.max_deliveries:
                self._dead_letter(
                    entry_id, raw, f"delivered {n} times (cap {self.max_deliveries})", counters
                )
            else:
                kept.append((entry_id, raw, msg))
        return kept

    def _dead_letter(self, entry_id, raw, error, counters: dict[str, int]) -> None:
        info = self.r.xpending_range(self.stream, self.group, min=entry_id, max=entry_id, count=1)
        n = info[0]["times_delivered"] if info else 0
        self.r.xadd(
            self.dlq,
            {"data": raw, "error": str(error)[:500], "source_id": entry_id, "deliveries": n},
        )
        self.r.xack(self.stream, self.group, entry_id)
        counters["dead_lettered"] += 1
        obs.DLQ.add(1, self._attrs)
        log.error("entry %s dead-lettered to %s: %s", entry_id, self.dlq, error)

    def close(self) -> None:
        obs.unregister_lag_reader(self.name)
        self.r.close()
