"""The columnar sink: one wide immutable row per observation.

Run-level properties are propagated onto every row — the layout whose
cost against a normalized join is the next slice's question. Writes
are idempotent on the event key, so a redelivered batch is free.

Decisions: D48 (SPEC.md).
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb

SINK_PATH = Path("data/ingest/observations.duckdb")

COLUMNS = (
    "event_key",
    "run_id",
    "seq",
    "kind",
    "ts",
    "latency_ms",
    "tokens",
    "cost_usd",
    "run_model",
    "run_git_ref",
    "run_started_at",
    "payload",
)

_DDL = """
CREATE TABLE IF NOT EXISTS observations (
  event_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  ts TIMESTAMP NOT NULL,
  latency_ms INTEGER,
  tokens INTEGER,
  cost_usd DOUBLE,
  run_model TEXT,
  run_git_ref TEXT,
  run_started_at TIMESTAMP,
  payload JSON
);
"""


class Sink:
    def __init__(self, path: Path | str = SINK_PATH) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        self._con.execute(_DDL)

    def write(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        before = self.count()
        columns = ",".join(COLUMNS)
        placeholders = ",".join("?" for _ in COLUMNS)
        self._con.executemany(
            f"INSERT INTO observations ({columns}) VALUES ({placeholders})"  # nosec B608
            " ON CONFLICT DO NOTHING",
            [[row.get(column) for column in COLUMNS] for row in rows],
        )
        return self.count() - before

    def count(self) -> int:
        row = self._con.execute("SELECT COUNT(*) FROM observations").fetchone()
        return int(row[0]) if row else 0

    def digest(self) -> str:
        """Order-independent fingerprint of the whole table — what a
        rebuilt sink has to match."""
        columns = ",".join(COLUMNS)
        rows = self._con.execute(
            f"SELECT {columns} FROM observations ORDER BY event_key"  # nosec B608
        ).fetchall()
        body = "\n".join(json.dumps([str(value) for value in row]) for row in rows)
        return hashlib.sha256(body.encode()).hexdigest()

    def reset(self) -> None:
        self._con.execute("DELETE FROM observations")

    def close(self) -> None:
        self._con.close()
