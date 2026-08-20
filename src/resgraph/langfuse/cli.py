"""resgraph-langfuse — export recorded runs, then read them back.

Decisions: D47 (SPEC.md).
"""

import base64
import json
import os
import sqlite3
from pathlib import Path

import httpx
import typer

from resgraph.langfuse import otlp, reconcile

app = typer.Typer(help="Langfuse integration: one-way export + the agent round-trip.")

DEFAULT_ENDPOINT = "http://localhost:3001"
DEFAULT_DB = Path("data/audit.db")


def _auth_headers() -> dict[str, str]:
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        raise SystemExit(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set: keys come from the "
            "environment, never from a file (same rule as the gateway accounts)"
        )
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "x-langfuse-ingestion-version": otlp.INGESTION_VERSION,
    }


def _load_run(db: Path, run_id: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    if not run_id:
        row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if row is None:
            raise SystemExit(f"no runs recorded in {db}")
        run_id = row["run_id"]
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"run {run_id!r} is not in {db}")
    events = conn.execute(
        "SELECT seq, kind, payload, latency_ms, tokens, ts FROM events WHERE run_id=? ORDER BY seq",
        (run_id,),
    ).fetchall()
    conn.close()
    return dict(run), [{**dict(e), "payload": json.loads(e["payload"])} for e in events]


@app.command()
def export(
    run_id: str = "",
    db: str = str(DEFAULT_DB),
    endpoint: str = DEFAULT_ENDPOINT,
    dry_run: bool = False,
) -> None:
    """Export one recorded run (default: the latest) as OTLP. The
    audit store stays the system of record; this is a one-way copy
    with the trail's own timestamps. --dry-run prints the document
    instead of sending it."""
    run, events = _load_run(Path(db), run_id)
    doc = otlp.run_to_otlp(run, events)
    if dry_run:
        typer.echo(json.dumps(doc, indent=1))
        return
    resp = httpx.post(
        f"{endpoint}{otlp.OTLP_TRACES_PATH}", json=doc, headers=_auth_headers(), timeout=30
    )
    if resp.status_code >= 300:
        raise SystemExit(f"export refused: HTTP {resp.status_code} {resp.text[:300]}")
    typer.echo(f"{run['run_id']}: {len(events)} events -> {endpoint} (HTTP {resp.status_code})")


@app.command()
def roundtrip(
    run_id: str = "",
    db: str = str(DEFAULT_DB),
    endpoint: str = DEFAULT_ENDPOINT,
    measure: str = "totalTokens",
) -> None:
    """The acceptance test: fetch the exported trace back through
    their read APIs — a row leg (observations) and an aggregate leg
    (metrics) — and reconcile both against the audit store. Exits
    nonzero on any named mismatch."""
    run, events = _load_run(Path(db), run_id)
    tid = otlp.trace_id(run["run_id"])
    headers = _auth_headers()
    obs = httpx.get(
        f"{endpoint}/api/public/v2/observations",
        params={"traceId": tid, "limit": 100},
        headers=headers,
        timeout=30,
    )
    if obs.status_code >= 300:
        raise SystemExit(f"row leg refused: HTTP {obs.status_code} {obs.text[:300]}")
    mismatches = reconcile.reconcile_rows(events, reconcile.observation_rows(obs.json()))
    query = {
        "view": "observations",
        "metrics": [{"measure": measure, "aggregation": "sum"}],
        "dimensions": [],
        "filters": [
            {"column": "traceId", "operator": "=", "value": tid, "type": "string"},
        ],
        "fromTimestamp": f"{run['started_at'][:10]}T00:00:00Z",
        "toTimestamp": f"{(run.get('finished_at') or run['started_at'])[:10]}T23:59:59Z",
    }
    agg = httpx.get(
        f"{endpoint}/api/public/v2/metrics",
        params={"query": json.dumps(query)},
        headers=headers,
        timeout=30,
    )
    if agg.status_code >= 300:
        raise SystemExit(f"aggregate leg refused: HTTP {agg.status_code} {agg.text[:300]}")
    rows = agg.json().get("data") or []
    mismatches += reconcile.reconcile_aggregate(run, rows, f"sum_{measure}")
    if mismatches:
        for m in mismatches:
            typer.echo(f"MISMATCH {m}")
        raise SystemExit(1)
    typer.echo(f"{run['run_id']}: both legs reconcile ({len(events)} events, measure {measure})")
