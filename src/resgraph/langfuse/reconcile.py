"""The agent round-trip: exported data read back and reconciled
against the audit store, every mismatch named.

Decisions: D47 (SPEC.md).
"""

import json
from typing import Any


def _by_seq(observations: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out = {}
    for obs in observations:
        seq = (obs.get("metadata") or {}).get("audit_seq")
        if seq is not None:
            out[int(seq)] = obs
    return out


def reconcile_rows(events: list[dict[str, Any]], observations: list[dict[str, Any]]) -> list[str]:
    """Every audit event against its fetched observation."""
    fetched = _by_seq(observations)
    mismatches = []
    for event in events:
        seq = event["seq"]
        obs = fetched.get(seq)
        if obs is None:
            mismatches.append(f"seq {seq} ({event['kind']}): no observation came back")
            continue
        want_ms = int(event.get("latency_ms") or 0)
        got_ms = obs.get("latency_ms")
        if got_ms is not None and abs(got_ms - want_ms) > 1:
            mismatches.append(f"seq {seq}: latency {got_ms}ms back vs {want_ms}ms recorded")
        if event["kind"] == "llm_call":
            want_tokens = event.get("tokens") or 0
            got_tokens = obs.get("total_tokens")
            if got_tokens is not None and got_tokens != want_tokens:
                mismatches.append(f"seq {seq}: {got_tokens} tokens back vs {want_tokens} recorded")
    extra = set(fetched) - {e["seq"] for e in events}
    for seq in sorted(extra):
        mismatches.append(f"seq {seq}: observation exists that the trail never wrote")
    return mismatches


def reconcile_aggregate(
    run: dict[str, Any], metrics_rows: list[dict[str, Any]], measure_field: str
) -> list[str]:
    """The run's own totals against the metrics answer for its window."""
    ours = (run.get("tokens_in") or 0) + (run.get("tokens_out") or 0)
    theirs = sum(int(float(row.get(measure_field) or 0)) for row in metrics_rows)
    if theirs != ours:
        return [f"aggregate: {theirs} tokens from their metrics vs {ours} from the runs table"]
    return []


def observation_rows(payload: Any) -> list[dict[str, Any]]:
    # metadata may arrive as a JSON string depending on the read API
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for row in rows or []:
        meta = row.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        out.append({**row, "metadata": meta or {}})
    return out
