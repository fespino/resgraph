"""Stream consumer — Redis stream in, idempotent apply out.

At-least-once by construction: entries are acknowledged only AFTER the
apply transaction commits. A crash between apply and ack means the entry
is redelivered on restart — and the watermark makes the reapplication a
no-op. Exactly-once *store state* on top of at-least-once *delivery*;
the consumer never needs to be careful, because the apply path already
is.

Recovery order: on start, drain this consumer's own pending entries
(delivered before a crash, never acked) before asking for new ones —
resuming is the same code path as running.

Poison entries (payloads that fail message validation) are counted,
logged, and acked: an unparseable entry would otherwise be redelivered
forever and wedge the stream. A dead-letter stream is the production
evolution if the count is ever nonzero — the generator provably emits
valid messages, so nonzero means transport corruption or a foreign
producer.
"""

import logging

from pydantic import ValidationError

from resgraph.schema import UpdateMessage

from .ingest import apply_message

log = logging.getLogger("resgraph.consumer")

DEFAULT_STREAM = "resgraph:updates"


class Consumer:
    def __init__(
        self,
        redis_url: str,
        session,
        stream: str = DEFAULT_STREAM,
        group: str = "resgraph-ingest",
        name: str = "c1",
        batch: int = 256,
        block_ms: int = 1000,
    ) -> None:
        import redis

        self.r = redis.Redis.from_url(redis_url)
        self.session = session
        self.stream = stream
        self.group = group
        self.name = name
        self.batch = batch
        self.block_ms = block_ms

    def ensure_group(self) -> None:
        """Create the consumer group at the stream's beginning (idempotent).

        id='0' so a group created after the producer started still sees
        every entry in the stream — the watermark makes overlap harmless,
        so erring toward re-reading is free.
        """
        import redis

        try:
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def run(self, max_messages: int | None = None, exit_on_idle: bool = False) -> dict[str, int]:
        """Consume until stopped. Returns counters — `applied` vs `skipped`
        is the watermark's scoreboard and the first thing to look at when
        deliveries were retried.
        """
        self.ensure_group()
        counters = {"read": 0, "applied": 0, "skipped": 0, "invalid": 0}
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
                self._apply_batch(entries, counters)
        except KeyboardInterrupt:
            pass  # an interrupt is a stop request; the counters still count
        return counters

    def _apply_batch(self, entries, counters: dict[str, int]) -> None:
        to_ack = []
        for entry_id, fields in entries:
            counters["read"] += 1
            try:
                msg = UpdateMessage.model_validate_json(fields[b"data"])
            except (ValidationError, KeyError) as e:
                counters["invalid"] += 1
                log.warning("poison entry %s acked and dropped: %s", entry_id, e)
                to_ack.append(entry_id)
                continue
            if apply_message(self.session, msg):
                counters["applied"] += 1
            else:
                counters["skipped"] += 1
            to_ack.append(entry_id)
        # ack strictly after the applies committed: crash before this line
        # redelivers the batch, and the watermark skips it — at-least-once
        # delivery, exactly-once state.
        self.r.xack(self.stream, self.group, *to_ack)

    def close(self) -> None:
        self.r.close()
