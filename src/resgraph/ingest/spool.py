"""Raw-first ingest: the batch lands in the spool, only its reference
is queued, and the producer's write path ends there.

Batches are content-addressed, so a re-sent batch overwrites nothing
and redelivery costs one file lookup. The queue redelivers by design
(a claim that is never acked is reclaimed), which is what makes the
sink's dedup load-bearing rather than defensive.

Decisions: D48 (SPEC.md).
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPOOL_ROOT = Path("data/ingest/raw")
QUEUE_PATH = Path("data/ingest/queue.db")

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS refs (
  ref TEXT PRIMARY KEY,
  enqueued_at REAL NOT NULL,
  claimed_at REAL
);
"""


@dataclass(frozen=True)
class Spool:
    root: Path = SPOOL_ROOT

    def write(self, batch: list[dict[str, Any]]) -> str:
        body = "".join(json.dumps(event, sort_keys=True) + "\n" for event in batch)
        ref = hashlib.sha256(body.encode()).hexdigest()[:16]
        path = self.root / f"{ref}.jsonl"
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return ref

    def read(self, ref: str) -> list[dict[str, Any]]:
        path = self.root / f"{ref}.jsonl"
        if not path.exists():
            raise FileNotFoundError(
                f"spool holds no batch {ref!r}: raw is the source, and it is gone"
            )
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def refs(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.jsonl"))


class RefQueue:
    def __init__(self, path: Path = QUEUE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_QUEUE_SCHEMA)

    def enqueue(self, ref: str, *, now: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO refs (ref, enqueued_at) VALUES (?,?) ON CONFLICT(ref) DO NOTHING",
            (ref, time.time() if now is None else now),
        )
        self._conn.commit()

    def claim(self, limit: int = 16, *, now: float | None = None) -> list[str]:
        stamp = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT ref FROM refs WHERE claimed_at IS NULL ORDER BY enqueued_at LIMIT ?",
            (limit,),
        ).fetchall()
        refs = [row[0] for row in rows]
        self._conn.executemany(
            "UPDATE refs SET claimed_at=? WHERE ref=?", [(stamp, ref) for ref in refs]
        )
        self._conn.commit()
        return refs

    def ack(self, ref: str) -> None:
        self._conn.execute("DELETE FROM refs WHERE ref=?", (ref,))
        self._conn.commit()

    def reclaim(self, older_than_s: float, *, now: float | None = None) -> list[str]:
        """Claims a worker took but never acked come back — the crash
        path, and the reason delivery is at-least-once."""
        cutoff = (time.time() if now is None else now) - older_than_s
        rows = self._conn.execute(
            "SELECT ref FROM refs WHERE claimed_at IS NOT NULL AND claimed_at <= ?", (cutoff,)
        ).fetchall()
        refs = [row[0] for row in rows]
        self._conn.executemany("UPDATE refs SET claimed_at=NULL WHERE ref=?", [(r,) for r in refs])
        self._conn.commit()
        return refs

    def pending(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM refs WHERE claimed_at IS NULL").fetchone()
        return int(row[0])

    def inflight(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM refs WHERE claimed_at IS NOT NULL"
        ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()
