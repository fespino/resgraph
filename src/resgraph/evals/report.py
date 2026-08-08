"""Aggregate run rows into the numbers the memo's bar is stated in.

Pure over a list of row dicts; the CLI owns file I/O. The headline is
the pass^k / pass@k pair: consistency across trials is the shipped
claim, best-of-k is the capability ceiling.
"""

from collections import defaultdict
from typing import Any

DIMS = ("found_top1", "found_top3", "evidence", "honesty", "discipline", "narrative")


def is_starved(row: dict[str, Any]) -> bool:
    return "budget_starved" in row.get("tags", [])


def is_store_degraded(row: dict[str, Any]) -> bool:
    return "store_degraded" in row.get("tags", [])


def item_passed(row: dict[str, Any]) -> bool:
    dims = row["dims"]
    if is_starved(row):
        # Starved items are graded on the cutoff contract, not on
        # finding the cause they cannot reach (D29).
        return bool(dims.get("cutoff", {}).get("passed"))
    if is_store_degraded(row):
        return bool(dims.get("degraded", {}).get("passed"))
    if row["scenario_type"] == "control":
        return bool(dims.get("honesty", {}).get("passed"))
    return bool(dims.get("found_top3", {}).get("passed")) and bool(
        dims.get("evidence", {}).get("passed")
    )


def _rate(hits: int, total: int) -> float | None:
    return hits / total if total else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_item: dict[str, list[bool]] = defaultdict(list)
    dim_hits: dict[str, list[bool]] = defaultdict(list)
    slice_hits: dict[str, list[bool]] = defaultdict(list)
    failed_dims: dict[str, set[str]] = defaultdict(set)
    fabrications: list[str] = []
    for row in rows:
        by_item[row["scenario_id"]].append(item_passed(row))
        # Starved items report in their own slice only: mixing them
        # into the causal-type slices would read a by-design "did not
        # find the cause under starvation" as a regression (D29).
        if is_starved(row):
            slice_hits["budget_starved"].append(item_passed(row))
        elif is_store_degraded(row):
            slice_hits["store_degraded"].append(item_passed(row))
        else:
            slice_hits[row["scenario_type"]].append(item_passed(row))
            slice_hits[f"source:{row.get('source', 'planted')}"].append(item_passed(row))
        for dim, res in row["dims"].items():
            dim_hits[dim].append(bool(res.get("passed")))
            if not res.get("passed"):
                failed_dims[row["scenario_id"]].add(dim)
            if dim == "evidence" and not res.get("passed"):
                fabrications.append(f"{row['scenario_id']}: {res.get('detail', '')}")
    latencies = [row["latency_s"] for row in rows if row.get("latency_s") is not None]
    cache_rates = [row["cache_hit_rate"] for row in rows if row.get("cache_hit_rate") is not None]
    trials = max((len(v) for v in by_item.values()), default=0)
    return {
        "items": len(by_item),
        # the gate compares item sets, not just counts
        "item_ids": sorted(by_item),
        "rows": len(rows),
        "trials": trials,
        "pass_all_trials": _rate(sum(all(v) for v in by_item.values()), len(by_item)),
        "pass_any_trial": _rate(sum(any(v) for v in by_item.values()), len(by_item)),
        "dims": {d: _rate(sum(hits), len(hits)) for d, hits in sorted(dim_hits.items())},
        "slices": {s: _rate(sum(hits), len(hits)) for s, hits in sorted(slice_hits.items())},
        # what a reviewer needs to act without leaving the PR (#152)
        "failing_items": [
            {"id": i, "dims": sorted(failed_dims[i])}
            for i in sorted(by_item)
            if not all(by_item[i])
        ],
        "fabrication_count": len(fabrications),
        "fabrications": fabrications,
        "degraded_rows": sum(bool(r.get("degraded")) for r in rows),
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p95_s": _percentile(latencies, 0.95),
        "cache_hit_mean": (sum(cache_rates) / len(cache_rates)) if cache_rates else None,
        "tokens_mean": (sum(r["tokens"]["total"] for r in rows) / len(rows) if rows else None),
        "fingerprints": sorted(
            {r["cache_fingerprint"] for r in rows if r.get("cache_fingerprint")}
        ),
    }


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _delta(current: float | None, base: float | None) -> str:
    if current is None or base is None:
        return ""
    return f" ({current - base:+.2f})"


def render(summary: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    base = baseline or {}
    lines = [
        f"items={summary['items']} trials={summary['trials']} rows={summary['rows']}",
        f"pass^k {_fmt(summary['pass_all_trials'])}"
        f"{_delta(summary['pass_all_trials'], base.get('pass_all_trials'))}"
        f"  pass@k {_fmt(summary['pass_any_trial'])}"
        f"{_delta(summary['pass_any_trial'], base.get('pass_any_trial'))}",
        f"fabrications={summary['fabrication_count']}"
        + (
            "  <-- HALT: iteration stops until this is zero" if summary["fabrication_count"] else ""
        ),
        f"degraded rows={summary['degraded_rows']}",
        f"latency p50={_fmt(summary['latency_p50_s'])}s p95={_fmt(summary['latency_p95_s'])}s"
        f"  cache={_fmt(summary['cache_hit_mean'])}",
        "",
        "dim rates:",
    ]
    base_dims = base.get("dims", {})
    for dim in DIMS:
        if dim in summary["dims"]:
            lines.append(
                f"  {dim:<12} {_fmt(summary['dims'][dim])}"
                f"{_delta(summary['dims'][dim], base_dims.get(dim))}"
            )
    lines.append("slices:")
    base_slices = base.get("slices", {})
    for s, rate in summary["slices"].items():
        lines.append(f"  {s:<16} {_fmt(rate)}{_delta(rate, base_slices.get(s))}")
    if len(summary["fingerprints"]) > 1:
        lines.append(f"WARNING: {len(summary['fingerprints'])} cache fingerprints in one run")
    return "\n".join(lines)
