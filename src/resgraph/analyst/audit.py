"""D27 — the audit trail: one writer, queryable with the agent stopped.

Two tables in embedded SQLite: `runs` (one row per triage run) and
`events` (llm_call / tool_call / step / approval / cutoff, ordered by
seq). The store of record holds payloads — metrics and traces carry
hashes and sizes, the audit store carries the arguments and the ids —
so the incident question ("what did the agent look at before it
proposed this?") is answered from this file alone. If answering needs
the agent re-run, it's logs, not an audit trail.

The harness feeds this through its `on_event` seam; the approval flow
and the step machine write their own kinds through the same writer.
SQLite over a composed service on purpose: the trail must outlive the
run and open anywhere, and laptop scale is the declared scale.
"""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .approval import ApprovalDecision
from .harness import RunResult
from .remediation import PlannedStep, StepEvent, StepStatus

DEFAULT_DB = Path("data") / "analyst-audit.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  alert TEXT NOT NULL,
  git_ref TEXT,
  model TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  tool_calls INTEGER,
  tokens_in INTEGER,
  tokens_out INTEGER,
  degraded INTEGER,
  verdict TEXT
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN
    ('llm_call','tool_call','step','approval','cutoff')),
  payload TEXT NOT NULL,
  latency_ms INTEGER,
  tokens INTEGER,
  ts TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);
"""


class AuditStore:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writing ------------------------------------------------------

    def begin_run(self, run_id: str, *, alert: dict[str, Any], model: str, git_ref: str) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, alert, git_ref, model, started_at) VALUES (?,?,?,?,?)",
            (run_id, json.dumps(alert), git_ref, model, _now()),
        )
        self._conn.commit()

    def finish_run(self, run_id: str, result: RunResult) -> None:
        verdicts = None
        if result.report is not None:
            verdicts = json.dumps(
                {
                    "suspects": len(result.report.suspects),
                    "no_confident_candidate": result.report.no_confident_candidate,
                }
            )
        self._conn.execute(
            "UPDATE runs SET finished_at=?, tool_calls=?, tokens_in=?, tokens_out=?,"
            " degraded=?, verdict=? WHERE run_id=?",
            (
                _now(),
                result.tool_calls,
                result.usage.total_input,
                result.usage.output_tokens,
                int(result.degraded),
                verdicts,
                run_id,
            ),
        )
        self._conn.commit()

    def sink(self, run_id: str) -> Callable[[str, dict[str, Any]], None]:
        """The harness's `on_event` callback: stamps seq + timestamp,
        lifts latency_ms/tokens out of the payload into their columns."""

        def write(kind: str, payload: dict[str, Any]) -> None:
            body = dict(payload)
            latency = body.pop("latency_ms", None)
            tokens = body.pop("tokens", None)
            self._append(run_id, kind, body, latency_ms=latency, tokens=tokens)

        return write

    def record_approval(self, run_id: str, decision: ApprovalDecision) -> None:
        self._append(
            run_id,
            "approval",
            {
                "approver": decision.approver,
                "approved": decision.approved,
                "plan_hash": decision.plan_hash,
                "applied": list(decision.applied),
                "skipped": list(decision.skipped),
            },
            latency_ms=decision.time_to_decision_ms,
        )

    def record_step_events(
        self, run_id: str, events: list[StepEvent], plan: list[PlannedStep]
    ) -> None:
        """StepEvents are progress surfaces and carry no arguments
        (D28); the audit row re-attaches the step's target so
        `--touched` answers from the store alone."""
        for e in events:
            self._append(
                run_id,
                "step",
                {
                    "tool": "apply_remediation",
                    "action": e.action,
                    "step_index": e.step_index,
                    "status": e.status.value,
                    "target": plan[e.step_index].target,
                    "error": e.error,
                },
                ts=e.timestamp,
            )

    def _append(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        latency_ms: int | None = None,
        tokens: int | None = None,
        ts: datetime | None = None,
    ) -> None:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE run_id=?", (run_id,)
        ).fetchone()
        self._conn.execute(
            "INSERT INTO events (run_id, seq, kind, payload, latency_ms, tokens, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, row[0], kind, json.dumps(payload), latency_ms, tokens, _iso(ts) or _now()),
        )
        self._conn.commit()

    # -- queries ------------------------------------------------------

    def timeline(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, kind, payload, latency_ms, tokens, ts FROM events"
            " WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            {
                "seq": seq,
                "kind": kind,
                "payload": json.loads(payload),
                "latency_ms": latency_ms,
                "tokens": tokens,
                "ts": ts,
            }
            for seq, kind, payload, latency_ms, tokens, ts in rows
        ]

    def touched(self, run_id: str) -> dict[str, list[str]]:
        """Distinct resources read (ids surfaced in tool results) and
        written (targets of steps that actually started)."""
        read: set[str] = set()
        written: set[str] = set()
        for event in self.timeline(run_id):
            p = event["payload"]
            if event["kind"] == "tool_call":
                read |= set(p.get("ids", ()))
            elif event["kind"] == "step" and p["status"] == StepStatus.STARTED.value:
                written.add(p["target"])
        return {"read": sorted(read), "written": sorted(written)}

    def tool_history(self, tool: str, *, since: timedelta | None = None) -> list[dict[str, Any]]:
        """Cross-run history for one tool; `apply_remediation` rows are
        the write history."""
        clauses = ["json_extract(payload, '$.tool') = ?"]
        params: list[Any] = [tool]
        if since is not None:
            clauses.append("ts >= ?")
            params.append(_iso(datetime.now(UTC) - since))
        rows = self._conn.execute(
            f"SELECT run_id, seq, kind, payload, ts FROM events"
            f" WHERE {' AND '.join(clauses)} ORDER BY ts",
            params,
        ).fetchall()
        return [
            {"run_id": run_id, "seq": seq, "kind": kind, "payload": json.loads(payload), "ts": ts}
            for run_id, seq, kind, payload, ts in rows
        ]

    def run_row(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        cols = [c[0] for c in self._conn.execute("SELECT * FROM runs LIMIT 0").description]
        return dict(zip(cols, row, strict=True))


def parse_since(text: str) -> timedelta:
    """`7d`, `24h`, `30m` — the CLI's --since grammar."""
    units = {"d": "days", "h": "hours", "m": "minutes"}
    unit = text[-1:]
    if unit not in units or not text[:-1].isdigit():
        raise ValueError(f"--since wants <n>d|<n>h|<n>m, got {text!r}")
    return timedelta(**{units[unit]: int(text[:-1])})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None
