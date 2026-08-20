"""Two layouts over the same rows, asked the same questions.

Wide: run properties propagated onto every observation. Normalized:
runs joined to events at read time. A third arm carries the duplicates
that at-least-once delivery leaves when the write path does NOT dedup,
so the read has to — which is the shape the wide-table claim is
usually measured against, and separating it says how much of any win
is the join and how much is the dedup.

The layouts are built from one row set and their answers are compared
before anything is timed: a comparison whose arms disagree measures
nothing.

Decisions: D49 (SPEC.md).
"""

import time
from pathlib import Path
from typing import Any

import duckdb

from resgraph.ingest.sink import Sink
from resgraph.ingest.worker import enrich, synth_batch

MODELS = ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8")

_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, model TEXT, git_ref TEXT, started_at TIMESTAMP
);
"""
_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
  event_key TEXT, run_id TEXT NOT NULL, seq INTEGER, kind TEXT,
  ts TIMESTAMP, latency_ms INTEGER, tokens INTEGER, cost_usd DOUBLE, payload JSON
);
"""

_PLAIN = "events"
_DEDUPED = "(SELECT * FROM events QUALIFY row_number() OVER (PARTITION BY event_key) = 1)"

_NORMALIZED: dict[str, str] = {
    "run_timeline": "SELECT seq, kind, latency_ms FROM {events} WHERE run_id='run-0' ORDER BY seq",
    "cost_by_model": (
        "SELECT r.model, ROUND(SUM(e.cost_usd), 8) FROM {events} e"
        " JOIN runs r USING (run_id) GROUP BY r.model ORDER BY r.model"
    ),
    "latency_p99_by_kind": (
        "SELECT kind, quantile_cont(latency_ms, 0.99) FROM {events} GROUP BY kind ORDER BY kind"
    ),
    "spend_per_run": (
        "SELECT e.run_id, r.model, ROUND(SUM(e.cost_usd), 8) FROM {events} e"
        " JOIN runs r USING (run_id) GROUP BY e.run_id, r.model"
        " ORDER BY e.run_id LIMIT 20"
    ),
}

_WIDE: dict[str, str] = {
    "run_timeline": (
        "SELECT seq, kind, latency_ms FROM observations WHERE run_id='run-0' ORDER BY seq"
    ),
    "cost_by_model": (
        "SELECT run_model, ROUND(SUM(cost_usd), 8) FROM observations"
        " GROUP BY run_model ORDER BY run_model"
    ),
    "latency_p99_by_kind": (
        "SELECT kind, quantile_cont(latency_ms, 0.99) FROM observations GROUP BY kind ORDER BY kind"
    ),
    "spend_per_run": (
        "SELECT run_id, run_model, ROUND(SUM(cost_usd), 8) FROM observations"
        " GROUP BY run_id, run_model ORDER BY run_id LIMIT 20"
    ),
}

QUERIES = tuple(_WIDE)


def sql_for(layout: str, query: str) -> str:
    if layout == "wide":
        return _WIDE[query]
    source = _DEDUPED if layout == "normalized_dedup" else _PLAIN
    return _NORMALIZED[query].format(events=source)


def wide_rows(runs: int, events_per_run: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(runs):
        model = MODELS[index % len(MODELS)]
        batch = synth_batch(f"run-{index}", events_per_run, model=model)
        rows.extend(enrich(event) for event in batch)
    return rows


def build_wide(path: Path, rows: list[dict[str, Any]]) -> None:
    sink = Sink(path)
    sink.write(rows)
    sink.close()


def build_normalized(path: Path, rows: list[dict[str, Any]], *, duplicate_every: int = 0) -> None:
    """Duplicate_every > 0 replays every Nth event, which is what
    at-least-once delivery leaves behind when nothing dedups on write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(_RUNS_DDL)
    con.execute(_EVENTS_DDL)
    runs = {
        (row["run_id"], row["run_model"], row["run_git_ref"], row["run_started_at"]) for row in rows
    }
    con.executemany("INSERT INTO runs VALUES (?,?,?,?)", [list(run) for run in sorted(runs)])
    narrow = [
        [
            row["event_key"],
            row["run_id"],
            row["seq"],
            row["kind"],
            row["ts"],
            row["latency_ms"],
            row["tokens"],
            row["cost_usd"],
            row["payload"],
        ]
        for row in rows
    ]
    if duplicate_every:
        narrow += [row for i, row in enumerate(narrow) if i % duplicate_every == 0]
    con.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", narrow)
    con.execute("CHECKPOINT")
    con.close()


def _time_queries(path: Path, layout: str, repeats: int) -> dict[str, float]:
    con = duckdb.connect(str(path), read_only=True)
    timings: dict[str, float] = {}
    for query in QUERIES:
        statement = sql_for(layout, query)
        con.execute(statement).fetchall()
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            con.execute(statement).fetchall()
            samples.append(time.perf_counter() - start)
        timings[query] = sorted(samples)[len(samples) // 2] * 1000
    con.close()
    return timings


def answers(path: Path, layout: str) -> dict[str, list[tuple[Any, ...]]]:
    con = duckdb.connect(str(path), read_only=True)
    out = {query: con.execute(sql_for(layout, query)).fetchall() for query in QUERIES}
    con.close()
    return out


def compare(
    root: Path,
    *,
    runs: int = 200,
    events_per_run: int = 30,
    repeats: int = 7,
    duplicate_every: int = 10,
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    rows = wide_rows(runs, events_per_run)
    paths = {
        "wide": root / "wide.duckdb",
        "normalized": root / "normalized.duckdb",
        "normalized_dedup": root / "normalized_dedup.duckdb",
    }
    for path in paths.values():
        path.unlink(missing_ok=True)
    build_wide(paths["wide"], rows)
    build_normalized(paths["normalized"], rows)
    build_normalized(paths["normalized_dedup"], rows, duplicate_every=duplicate_every)

    reference = answers(paths["wide"], "wide")
    for layout in ("normalized", "normalized_dedup"):
        got = answers(paths[layout], layout)
        for query in QUERIES:
            if got[query] != reference[query]:
                raise SystemExit(
                    f"{layout} disagrees with wide on {query}: the layouts hold different "
                    "data, so any timing between them would measure nothing"
                )
    return {
        "rows": len(rows),
        "runs": runs,
        "layouts": {
            layout: {
                "bytes": path.stat().st_size,
                "queries": _time_queries(path, layout, repeats),
            }
            for layout, path in paths.items()
        },
    }
