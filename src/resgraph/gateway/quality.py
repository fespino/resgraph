"""The quality table: per-task-class scores the router may consume.

Every entry carries the run file and date it came from and is refused
without them — a routing table with no provenance is an opinion. The
table is generated from eval runs (resgraph-evals routing-table),
never hand-written.

Decisions: D44, D51, D52 (SPEC.md).
"""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

QUALITY_PATH = Path("evals/routing-quality.yaml")


REQUIRED = ("passk", "fabrication_count", "run", "date")


def _optional_float(entry: dict[str, Any], key: str) -> float | None:
    value = entry.get(key)
    return float(value) if value is not None else None


def load_quality(text: str) -> dict[str, dict[str, dict[str, Any]]]:
    """task_class -> alias -> {passk, cost_per_passed, latencies,
    fabrication_count, workers, run, date}."""
    doc = yaml.safe_load(text) or {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for task_class, entries in (doc.get("scores") or {}).items():
        out[task_class] = {}
        for alias, entry in (entries or {}).items():
            entry = entry or {}
            missing = [k for k in REQUIRED if not entry.get(k) and entry.get(k) != 0]
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
                "cost_per_passed": _optional_float(entry, "cost_per_passed"),
                "latency_p50_s": _optional_float(entry, "latency_p50_s"),
                "latency_p95_s": _optional_float(entry, "latency_p95_s"),
                "fabrication_count": int(entry["fabrication_count"]),
                "workers": [str(w) for w in (entry.get("workers") or [])],
                "run": str(entry["run"]),
                "date": str(entry["date"]),
            }
    return out


def fabricating(
    table: dict[str, dict[str, dict[str, Any]]], task_class: str, candidates: list[str]
) -> list[str]:
    """Candidates whose measured run contains a fabrication. The eval
    gate blocks a merge on these unconditionally (D29b); routing them
    anyway would spend on a worker the rest of the platform treats as
    disqualified rather than merely weaker."""
    scores = table.get(task_class, {})
    return [a for a in candidates if a in scores and scores[a]["fabrication_count"]]


def eligible(
    table: dict[str, dict[str, dict[str, Any]]],
    task_class: str,
    candidates: list[str],
    min_passk: float,
) -> list[str]:
    """Candidates whose measured pass^k clears the floor and whose run
    fabricated nothing. An unmeasured candidate is ineligible — no
    eval, no route: the floor is a guarantee, and a guarantee cannot
    rest on an absent measurement."""
    scores = table.get(task_class, {})
    disqualified = set(fabricating(table, task_class, candidates))
    return [
        a
        for a in candidates
        if a in scores and scores[a]["passk"] >= min_passk and a not in disqualified
    ]


# (axis name, lower_is_better) — an axis absent from a score is not
# compared, so a table that never carried latency behaves as before
AXES: tuple[tuple[str, bool], ...] = (
    ("passk", False),
    ("cost_per_passed", True),
    ("latency_p50_s", True),
    ("latency_p95_s", True),
)


def dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when a is at least as good as b on every axis both record
    and strictly better on one. Serving an arm never updates its
    pass^k — there is no answer key at request time — so an arm that
    loses everywhere cannot earn its way back by being served."""
    comparable = [(k, lower) for k, lower in AXES if a.get(k) is not None and b.get(k) is not None]
    if not comparable:
        return False
    better_somewhere = False
    for key, lower_is_better in comparable:
        x, y = float(a[key]), float(b[key])
        if lower_is_better:
            if x > y:
                return False
            better_somewhere |= x < y
        else:
            if x < y:
                return False
            better_somewhere |= x > y
    return better_somewhere


def frontier(scores: dict[str, dict[str, Any]], candidates: list[str]) -> list[str]:
    """The candidates no other candidate dominates, order preserved."""
    known = [a for a in candidates if a in scores]
    return [a for a in known if not any(dominates(scores[o], scores[a]) for o in known if o != a)]


def stale(
    scores: dict[str, dict[str, Any]], candidates: list[str], today: str, max_age_days: int
) -> list[str]:
    """Arms whose evidence is older than the bound. Staleness asks for
    a re-measurement, never for a share of traffic: a served request
    produces no quality signal, so routing to a stale arm buys no
    information — unlike the dispatch layer, where serving updates the
    latency window it is ranked by."""
    cutoff = date.fromisoformat(today) - timedelta(days=max_age_days)
    out = []
    for alias in candidates:
        entry = scores.get(alias)
        if entry is None:
            continue
        try:
            measured = date.fromisoformat(str(entry["date"]))
        except ValueError:
            continue
        if measured < cutoff:
            out.append(alias)
    return out
