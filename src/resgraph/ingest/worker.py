"""The async leg: claim a reference, read raw, enrich, flush, ack.

Ack lands after the write, never before — a worker that dies mid-batch
leaves the claim to be reclaimed and the batch redelivered, which the
sink's idempotent write absorbs. Raw is authoritative: the sink can be
dropped and rebuilt from the spool alone.

Decisions: D48 (SPEC.md).
"""

import json
import time
from typing import Any

from resgraph.evals.pricing import estimate_cost
from resgraph.ingest.sink import Sink
from resgraph.ingest.spool import RefQueue, Spool


def enrich(event: dict[str, Any]) -> dict[str, Any]:
    """One raw event as a wide row: run properties propagated, cost
    priced from the tokens the trail recorded."""
    run = event.get("run") or {}
    payload = event.get("payload") or {}
    tokens = {
        "input": int(payload.get("input_tokens") or 0),
        "output": int(payload.get("output_tokens") or 0),
        "cache_read": int(payload.get("cache_read_tokens") or 0),
        "cache_creation": int(payload.get("cache_creation_tokens") or 0),
    }
    return {
        "event_key": f"{event['run_id']}:{event['seq']}",
        "run_id": event["run_id"],
        "seq": int(event["seq"]),
        "kind": event["kind"],
        "ts": event["ts"],
        "latency_ms": event.get("latency_ms"),
        "tokens": event.get("tokens"),
        "cost_usd": estimate_cost(tokens, str(run.get("model") or "")),
        "run_model": run.get("model"),
        "run_git_ref": run.get("git_ref"),
        "run_started_at": run.get("started_at"),
        "payload": json.dumps(payload, sort_keys=True),
    }


def drain(spool: Spool, queue: RefQueue, sink: Sink, *, limit: int = 16) -> dict[str, int]:
    """One worker pass over whatever is claimable."""
    refs = queue.claim(limit)
    written = 0
    for ref in refs:
        rows = [enrich(event) for event in spool.read(ref)]
        written += sink.write(rows)
        queue.ack(ref)
    return {"batches": len(refs), "rows_written": written}


def replay_from_raw(spool: Spool, sink: Sink) -> dict[str, int]:
    """The loss proof: drop the sink, rebuild it from raw alone."""
    sink.reset()
    written = 0
    refs = spool.refs()
    for ref in refs:
        written += sink.write([enrich(event) for event in spool.read(ref)])
    return {"batches": len(refs), "rows_written": written}


def synth_batch(run_id: str, size: int, *, model: str = "claude-haiku-4-5") -> list[dict[str, Any]]:
    """Deterministic audit-shaped events for the measurement — no
    recorded run is needed to exercise the pipeline's properties."""
    run = {"model": model, "git_ref": "0000000", "started_at": "2026-08-20T00:00:00"}
    batch = []
    for seq in range(size):
        llm = seq % 3 == 0
        batch.append(
            {
                "run_id": run_id,
                "seq": seq,
                "kind": "llm_call" if llm else "tool_call",
                "ts": f"2026-08-20T00:{seq // 60:02d}:{seq % 60:02d}",
                "latency_ms": 1500 if llm else 20,
                "tokens": 1000 if llm else None,
                "payload": (
                    {"turn": seq, "input_tokens": 900, "output_tokens": 100}
                    if llm
                    else {"tool": "fetch_resource", "ok": True}
                ),
                "run": run,
            }
        )
    return batch


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)

    def at(percentile: int) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * percentile / 100))]

    return {"p50_us": at(50) * 1e6, "p99_us": at(99) * 1e6, "max_us": ordered[-1] * 1e6}


def measure_backpressure(
    spool: Spool, queue: RefQueue, sink: Sink, *, batches: int, size: int
) -> dict[str, Any]:
    """The claim the queue exists for: with the worker stalled, the
    producer's own write path does not move. The direct arm is timed
    generously — enrichment sits outside its clock, so it measures the
    insert alone."""
    queued: list[float] = []
    direct: list[float] = []
    for i in range(batches):
        batch = synth_batch(f"queued-{i}", size)
        start = time.perf_counter()
        queue.enqueue(spool.write(batch))
        queued.append(time.perf_counter() - start)

        rows = [enrich(event) for event in synth_batch(f"direct-{i}", size)]
        start = time.perf_counter()
        sink.write(rows)
        direct.append(time.perf_counter() - start)
    queued_stats, direct_stats = _percentiles(queued), _percentiles(direct)
    half = len(queued) // 2
    early = _percentiles(queued[:half])["p50_us"] if half else queued_stats["p50_us"]
    late = _percentiles(queued[half:])["p50_us"] if half else queued_stats["p50_us"]
    return {
        "batches": batches,
        "batch_size": size,
        "queued": queued_stats,
        "direct": direct_stats,
        "backlog": queue.pending(),
        "speedup_p50": direct_stats["p50_us"] / queued_stats["p50_us"],
        # the isolation property itself: an empty backlog against a full one
        "queued_early_p50_us": early,
        "queued_late_p50_us": late,
        "backlog_drift": late / early,
    }
