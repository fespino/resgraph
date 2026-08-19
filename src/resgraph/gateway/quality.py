"""The quality table: per-task-class scores the router may consume.

Every entry carries the run file and date it came from and is refused
without them — a routing table with no provenance is an opinion. The
table is generated from eval runs (resgraph-evals routing-table),
never hand-written.

Decisions: D44 (SPEC.md).
"""

from pathlib import Path
from typing import Any

import yaml

QUALITY_PATH = Path("evals/routing-quality.yaml")


def load_quality(text: str) -> dict[str, dict[str, dict[str, Any]]]:
    """task_class -> alias -> {passk, cost_per_passed, run, date}."""
    doc = yaml.safe_load(text) or {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for task_class, entries in (doc.get("scores") or {}).items():
        out[task_class] = {}
        for alias, entry in (entries or {}).items():
            entry = entry or {}
            missing = [
                k for k in ("passk", "run", "date") if not entry.get(k) and entry.get(k) != 0
            ]
            if missing:
                raise SystemExit(
                    f"quality entry {task_class}/{alias} lacks {missing}: "
                    "a score without provenance is an opinion, not a measurement"
                )
            passk = float(entry["passk"])
            if not 0.0 <= passk <= 1.0:
                raise SystemExit(f"quality entry {task_class}/{alias}: passk {passk} not in [0,1]")
            out[task_class][alias] = {
                "passk": passk,
                "cost_per_passed": (
                    float(entry["cost_per_passed"])
                    if entry.get("cost_per_passed") is not None
                    else None
                ),
                "run": str(entry["run"]),
                "date": str(entry["date"]),
            }
    return out


def eligible(
    table: dict[str, dict[str, dict[str, Any]]],
    task_class: str,
    candidates: list[str],
    min_passk: float,
) -> list[str]:
    """Candidates whose measured pass^k clears the floor. An unmeasured
    candidate is ineligible — no eval, no route: the floor is a
    guarantee, and a guarantee cannot rest on an absent measurement."""
    scores = table.get(task_class, {})
    return [a for a in candidates if a in scores and scores[a]["passk"] >= min_passk]
