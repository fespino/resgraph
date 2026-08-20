"""A recorded triage run as one OTLP document, for Langfuse.

Raw OTLP JSON, not their SDK: the legacy ingestion API sunsets
2026-11-16, and OTLP spans carry explicit timestamps — an exported
run keeps the trail's clock, never ours. Generations carry usage but
never cost: the two-meters comparison needs Langfuse to price traffic
with its own table.

Decisions: D47 (SPEC.md).
"""

import hashlib
import json
from datetime import datetime
from typing import Any

OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
INGESTION_VERSION = "4"

_KIND_TYPES = {"llm_call": "generation", "tool_call": "tool", "step": "event"}


def trace_id(run_id: str) -> str:
    return hashlib.sha256(f"run:{run_id}".encode()).hexdigest()[:32]


def span_id(run_id: str, seq: int) -> str:
    return hashlib.sha256(f"span:{run_id}:{seq}".encode()).hexdigest()[:16]


def to_nanos(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1_000_000_000)


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _usage(event: dict[str, Any]) -> dict[str, int]:
    # the split only when the trail recorded it — never invented from a total
    payload = event["payload"]
    if "input_tokens" in payload:
        return {"input": payload["input_tokens"], "output": payload["output_tokens"]}
    return {"total": event.get("tokens") or 0}


def _event_span(run_id: str, model: str, event: dict[str, Any]) -> dict[str, Any]:
    kind, seq = event["kind"], event["seq"]
    start = to_nanos(event["ts"])
    end = start + int(event.get("latency_ms") or 0) * 1_000_000
    attrs = [_attr("langfuse.observation.type", _KIND_TYPES.get(kind, "event"))]
    if kind == "llm_call":
        attrs.append(_attr("langfuse.observation.model.name", model))
        attrs.append(_attr("langfuse.observation.usage_details", json.dumps(_usage(event))))
    name = event["payload"].get("tool") if kind == "tool_call" else None
    attrs.append(_attr("langfuse.observation.metadata.audit_seq", seq))
    attrs.append(_attr("langfuse.observation.output", json.dumps(event["payload"])))
    return {
        "traceId": trace_id(run_id),
        "spanId": span_id(run_id, seq),
        "parentSpanId": span_id(run_id, -1),
        "name": name or f"{kind}[{seq}]",
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attrs,
    }


def run_to_otlp(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Root span for the run, one child span per audit event."""
    rid = run["run_id"]
    starts = [to_nanos(e["ts"]) for e in events]
    ends = [to_nanos(e["ts"]) + int(e.get("latency_ms") or 0) * 1_000_000 for e in events]
    root = {
        "traceId": trace_id(rid),
        "spanId": span_id(rid, -1),
        "name": f"triage:{rid}",
        "startTimeUnixNano": str(
            to_nanos(run["started_at"]) if run.get("started_at") else min(starts)
        ),
        "endTimeUnixNano": str(
            to_nanos(run["finished_at"]) if run.get("finished_at") else max(ends)
        ),
        "attributes": [
            _attr("langfuse.session.id", rid),
            _attr("langfuse.trace.name", f"triage:{rid}"),
            _attr("langfuse.trace.metadata.model", run.get("model") or ""),
            _attr("langfuse.trace.metadata.git_ref", run.get("git_ref") or ""),
            _attr(
                "langfuse.trace.metadata.usage",
                json.dumps(
                    {"input": run.get("tokens_in") or 0, "output": run.get("tokens_out") or 0}
                ),
            ),
        ],
    }
    spans = [root] + [_event_span(rid, run.get("model") or "", e) for e in events]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "resgraph-analyst")]},
                "scopeSpans": [{"scope": {"name": "resgraph.langfuse"}, "spans": spans}],
            }
        ]
    }
