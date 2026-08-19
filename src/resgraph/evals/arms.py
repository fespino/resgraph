"""Compare labeled arms of the same suite: cost, latency, and pass rate
per arm.

`report` aggregates one run and `gate` compares a run to the certified
baseline; neither answers "which worker earns its price." An arm is a
run of the same item set under one changed variable (worker model, or
the skill on/off). The money metric is worker cost divided by the
triages that actually passed at k; latency rides alongside it, because a
cheaper worker that is far slower is a different trade, not a free win.

Decisions: D29c (SPEC.md).
"""

from collections import defaultdict
from typing import Any

from .report import aggregate, item_passed
from .runner import estimate_cost


def arm_summary(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    agg = aggregate(rows)
    worker_cost = sum(estimate_cost(r["tokens"], r["model"]) for r in rows)
    passed_items = sum(all(v) for v in _by_item(rows).values())
    return {
        "label": label,
        "items": agg["items"],
        "item_ids": agg["item_ids"],
        "trials": agg["trials"],
        "pass_all_trials": agg["pass_all_trials"],
        "pass_any_trial": agg["pass_any_trial"],
        "fabrication_count": agg["fabrication_count"],
        "slices": agg["slices"],
        "worker_cost": worker_cost,
        "passed_items": passed_items,
        "cost_per_passed": worker_cost / passed_items if passed_items else None,
        "latency_p50_s": agg["latency_p50_s"],
        "latency_p95_s": agg["latency_p95_s"],
        "models": sorted({r["model"] for r in rows if r.get("model")}),
    }


def _by_item(rows: list[dict[str, Any]]) -> dict[str, list[bool]]:
    by_item: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        by_item[row["scenario_id"]].append(item_passed(row))
    return by_item


def compare(arms: list[dict[str, Any]], baseline: str) -> dict[str, Any]:
    """Deltas against the baseline arm. Declines if the arms do not
    cover the same item set — a cost comparison across different work
    is meaningless, the same rule the gate applies to baselines."""
    base = next((a for a in arms if a["label"] == baseline), None)
    if base is None:
        raise ValueError(f"baseline arm {baseline!r} not among {[a['label'] for a in arms]}")
    base_ids = set(base["item_ids"])
    mismatched = [a["label"] for a in arms if set(a["item_ids"]) != base_ids]
    comparable = not mismatched
    deltas = {}
    for a in arms:
        if a["label"] == baseline:
            continue
        d = None
        if a["pass_all_trials"] is not None and base["pass_all_trials"] is not None:
            d = a["pass_all_trials"] - base["pass_all_trials"]
        deltas[a["label"]] = d
    return {
        "baseline": baseline,
        "comparable": comparable,
        "mismatched": mismatched,
        "deltas": deltas,
    }


def _fmt(v: float | None, places: int = 3) -> str:
    return "—" if v is None else f"{v:.{places}f}"


def render(arms: list[dict[str, Any]], comparison: dict[str, Any]) -> str:
    lines = [
        f"arms={len(arms)} baseline={comparison['baseline']} comparable={comparison['comparable']}",
    ]
    if not comparison["comparable"]:
        lines.append(f"  DECLINED: item sets differ ({', '.join(comparison['mismatched'])})")
    header = (
        f"  {'arm':<10} {'pass^k':>7} {'Δ':>7} {'cost$':>8} {'$/passed':>9} "
        f"{'p50s':>6} {'p95s':>6} {'fab':>4}"
    )
    lines.append(header)
    for a in arms:
        d = comparison["deltas"].get(a["label"])
        lines.append(
            f"  {a['label']:<10} {_fmt(a['pass_all_trials'], 2):>7} "
            f"{('' if d is None else f'{d:+.2f}'):>7} {_fmt(a['worker_cost'], 2):>8} "
            f"{_fmt(a['cost_per_passed'], 3):>9} {_fmt(a['latency_p50_s'], 1):>6} "
            f"{_fmt(a['latency_p95_s'], 1):>6} {a['fabrication_count']:>4}"
        )
    return "\n".join(lines)
