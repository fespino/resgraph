"""The review queue + the flywheel (D39): the labels are the product.

Embedded SQLite like the audit store (D27's rationale: a queue that
depends on a service being up loses labels exactly when it matters).
The reviewer sees the triggering evidence, not the haystack; every
decision closes a loop in code — a false positive becomes a scoped
rule exclusion plus a must-stay-clean regression entry, a confirmed
catch joins the recall floor.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB_PATH = Path("data/sentinel-reviews.db")
EXCLUSIONS_PATH = Path("evals/sentinel/exclusions.jsonl")
CONFIRMED_PATH = Path("evals/sentinel/confirmed.jsonl")

STATUSES = ("pending", "confirmed_malicious", "false_positive", "unclear")

_SCHEMA = """CREATE TABLE IF NOT EXISTS reviews (
    run_key TEXT PRIMARY KEY,
    enqueued_at TEXT NOT NULL,
    evidence TEXT NOT NULL,
    l3_tag TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    decided_at TEXT,
    seconds_to_decision REAL
)"""


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Decision:
    run_key: str
    status: str
    exclusions_added: int
    confirmed_added: int


class ReviewStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def enqueue(self, run_key: str, evidence: list[str], l3_tag: str | None) -> None:
        """Idempotent: re-enqueueing a decided run must not reopen it."""
        self._db.execute(
            "INSERT OR IGNORE INTO reviews (run_key, enqueued_at, evidence, l3_tag) "
            "VALUES (?, ?, ?, ?)",
            (run_key, _now().isoformat(), json.dumps(evidence), l3_tag),
        )
        self._db.commit()

    def pending(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT run_key, enqueued_at, evidence, l3_tag FROM reviews "
            "WHERE status = 'pending' ORDER BY enqueued_at"
        ).fetchall()
        return [
            {"run_key": k, "enqueued_at": t, "evidence": json.loads(e), "l3_tag": tag}
            for k, t, e, tag in rows
        ]

    def decide(
        self,
        run_key: str,
        status: str,
        reviewer: str,
        *,
        exclusions_path: Path | None = None,
        confirmed_path: Path | None = None,
    ) -> Decision:
        if status not in STATUSES or status == "pending":
            raise SystemExit(f"status must be one of {STATUSES[1:]}")
        row = self._db.execute(
            "SELECT enqueued_at, evidence, status FROM reviews WHERE run_key = ?", (run_key,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"{run_key!r} is not in the queue")
        enqueued_at, evidence_json, prior = row
        if prior != "pending":
            raise SystemExit(f"{run_key!r} already decided ({prior}); decisions are append-only")
        decided = _now()
        seconds = (decided - datetime.fromisoformat(enqueued_at)).total_seconds()
        self._db.execute(
            "UPDATE reviews SET status=?, reviewer=?, decided_at=?, seconds_to_decision=? "
            "WHERE run_key=?",
            (status, reviewer, decided.isoformat(), seconds, run_key),
        )
        self._db.commit()
        evidence = json.loads(evidence_json)
        excl = conf = 0
        if status == "false_positive":
            excl = _append_exclusions(run_key, evidence, exclusions_path or EXCLUSIONS_PATH)
        elif status == "confirmed_malicious":
            conf = _append_confirmed(run_key, evidence, confirmed_path or CONFIRMED_PATH)
        return Decision(run_key=run_key, status=status, exclusions_added=excl, confirmed_added=conf)

    def decided(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT run_key, status, reviewer, seconds_to_decision FROM reviews "
            "WHERE status != 'pending' ORDER BY decided_at"
        ).fetchall()
        return [
            {"run_key": k, "status": s, "reviewer": r, "seconds_to_decision": sec}
            for k, s, r, sec in rows
        ]


def _rules_in(evidence: list[str]) -> list[str]:
    return [e.split(":", 1)[0] for e in evidence if ":" in e and not e.startswith("vs ")]


def _append_exclusions(run_key: str, evidence: list[str], path: Path) -> int:
    """A scoped exclusion per (rule, run) — never a rule disable: the
    rule stays live everywhere else, and the carve-out is a reviewable
    committed diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rules = _rules_in(evidence) or ["l2_anomaly"]
    with path.open("a") as f:
        for rule in rules:
            f.write(json.dumps({"rule": rule, "run_key": run_key}, sort_keys=True) + "\n")
    return len(rules)


def _append_confirmed(run_key: str, evidence: list[str], path: Path) -> int:
    """A confirmed catch joins the recall floor by reference — this
    attack shape must be caught forever."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"run_key": run_key, "evidence": evidence}, sort_keys=True) + "\n")
    return 1


def load_exclusions(path: Path | None = None) -> set[tuple[str, str]]:
    p = path or EXCLUSIONS_PATH
    if not p.exists():
        return set()
    return {
        (row["rule"], row["run_key"]) for row in map(json.loads, p.read_text().splitlines()) if row
    }


def load_confirmed(path: Path | None = None) -> set[str]:
    p = path or CONFIRMED_PATH
    if not p.exists():
        return set()
    return {row["run_key"] for row in map(json.loads, p.read_text().splitlines()) if row}
