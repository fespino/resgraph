"""Two counts, one truth: the audit store against the sink.

Errors and dashboards cannot see a producer that simply stopped —
every component reports healthy because nothing is arriving to fail.
The control is a second count from a source the pipeline does not
own: what the trail recorded, against what the sink holds.

Decisions: D48 (SPEC.md).
"""

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb


def _audit_counts(audit_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{audit_path}?mode=ro", uri=True)
    rows = conn.execute("SELECT run_id, COUNT(*) FROM events GROUP BY run_id").fetchall()
    conn.close()
    return {str(run): int(count) for run, count in rows}


def _sink_counts(sink_path: Path) -> dict[str, int]:
    con = duckdb.connect(str(sink_path), read_only=True)
    rows = con.execute("SELECT run_id, COUNT(*) FROM observations GROUP BY run_id").fetchall()
    con.close()
    return {str(run): int(count) for run, count in rows}


def reconcile(audit_path: Path, sink_path: Path) -> list[str]:
    """Every recorded run against what the sink holds, per run."""
    recorded, held = _audit_counts(audit_path), _sink_counts(sink_path)
    findings = []
    for run in sorted(set(recorded) | set(held)):
        theirs, ours = recorded.get(run), held.get(run)
        if ours is None:
            findings.append(f"{run}: {theirs} events recorded, nothing in the sink")
        elif theirs is None:
            findings.append(f"{run}: {ours} rows in the sink the trail never recorded")
        elif theirs != ours:
            findings.append(f"{run}: {theirs} events recorded, {ours} rows in the sink")
    return findings


def silence_seconds(audit_path: Path, *, now: float | None = None) -> float | None:
    """Age of the newest recorded event. Reconciliation agrees at zero
    when a producer stops, so absence needs its own question."""
    conn = sqlite3.connect(f"file:{audit_path}?mode=ro", uri=True)
    row = conn.execute("SELECT MAX(ts) FROM events").fetchone()
    conn.close()
    if row is None or row[0] is None:
        return None
    newest = datetime.fromisoformat(str(row[0]))
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    return (time.time() if now is None else now) - newest.timestamp()
