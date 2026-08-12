"""D29b — the CI eval gate: block an eval regression before it merges.

Pure over two `report.aggregate` summaries; no I/O, no API calls. Block
rules and their rationale are recorded in SPEC.md (D29b). The
`eval-baseline-refresh` label override is enforced by the CI job, not
here; fabrications block regardless.
"""

from dataclasses import dataclass, field
from typing import Any

OVERALL_DROP = 0.02
SLICE_DROP = 0.05
MIN_TRIALS = 3
PROTECTED_SLICES = ("source:failure_derived", "budget_starved", "store_degraded", "injection")

# Companion sets carry their own dataset and slice; a run made entirely
# of them is never a candidate against the base baseline. A base run
# whose dataset drifted is NOT companion-only, so it is still selected
# and declines loudly rather than being skipped.
COMPANION_TAGS = frozenset({"budget_starved", "store_degraded", "injection", "coverage_gap"})


def is_companion_only(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(any(t in COMPANION_TAGS for t in row.get("tags", [])) for row in rows)


def _run_trials(rows: list[dict[str, Any]]) -> int:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["scenario_id"]] = counts.get(row["scenario_id"], 0) + 1
    return max(counts.values(), default=0)


def _row_model(rows: list[dict[str, Any]]) -> str | None:
    models = {r.get("model") for r in rows if r.get("model")}
    return next(iter(models)) if len(models) == 1 else None


def gate_skip_reason(rows: list[dict[str, Any]], baseline_model: str | None = None) -> str:
    """Empty when this run is a gate candidate; otherwise why it is not.
    Companion sets, sub-k runs, and runs of a different worker than the
    baseline are all "not a gate candidate" (D29b/D29c), so selection skips
    past them to find the newest run it can verdict — a weaker local worker is
    an `arms` comparison, not a regression against the certified baseline."""
    if not rows:
        return "empty"
    if is_companion_only(rows):
        return "companion-set"
    trials = _run_trials(rows)
    if trials < MIN_TRIALS:
        return f"k={trials}<{MIN_TRIALS}"
    run_model = _row_model(rows)
    if baseline_model and run_model and run_model != baseline_model:
        return f"worker {run_model} != baseline {baseline_model}"
    return ""


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    undecided: bool = False
    undecided_reason: str = ""
    run: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    # judged against these bars; rendering reads them so it cannot differ
    overall_drop: float = OVERALL_DROP
    slice_drop: float = SLICE_DROP


def evaluate(
    run: dict[str, Any],
    baseline: dict[str, Any],
    *,
    overall_drop: float = OVERALL_DROP,
    slice_drop: float = SLICE_DROP,
    min_trials: int = MIN_TRIALS,
) -> GateVerdict:
    """Compare an aggregated run against the baseline. `run` and
    `baseline` are `report.aggregate` outputs."""
    reason = (
        _not_comparable(run, baseline)
        or _too_few_trials(run, min_trials)
        or _worker_mismatch(run, baseline)
    )
    if reason:
        return GateVerdict(
            passed=False,
            undecided=True,
            undecided_reason=reason,
            run=run,
            baseline=baseline,
            overall_drop=overall_drop,
            slice_drop=slice_drop,
        )

    blocks: list[str] = []
    warnings: list[str] = []

    fab = run.get("fabrication_count") or 0
    if fab:
        detail = "; ".join(run.get("fabrications", [])) or "see run rows"
        blocks.append(f"{fab} fabrication(s) — unconditional block: {detail}")

    drop = _drop(run.get("pass_all_trials"), baseline.get("pass_all_trials"))
    if drop is not None and drop > overall_drop:
        blocks.append(
            f"overall pass^k dropped {drop:.3f} "
            f"({_fmt(baseline.get('pass_all_trials'))} -> "
            f"{_fmt(run.get('pass_all_trials'))}), over the {overall_drop:.2f} bar"
        )

    run_slices = run.get("slices", {})
    base_slices = baseline.get("slices", {})
    for name in sorted(run_slices):
        if name not in base_slices:
            warnings.append(_new_slice_warning(name))
            continue
        s_drop = _drop(run_slices[name], base_slices[name])
        if s_drop is not None and s_drop > slice_drop:
            label = "regression slice " if name == "source:failure_derived" else "slice "
            blocks.append(
                f"{label}{name!r} dropped {s_drop:.3f} "
                f"({base_slices[name]:.2f} -> {run_slices[name]:.2f}), "
                f"over the {slice_drop:.2f} bar"
            )
    for name in sorted(base_slices):
        if name not in run_slices:
            warnings.append(f"slice {name!r} present in baseline but absent from the run")

    return GateVerdict(
        passed=not blocks,
        blocks=blocks,
        warnings=warnings,
        run=run,
        baseline=baseline,
        overall_drop=overall_drop,
        slice_drop=slice_drop,
    )


def _not_comparable(run: dict[str, Any], baseline: dict[str, Any]) -> str:
    run_ids, base_ids = run.get("item_ids"), baseline.get("item_ids")
    if run_ids is None or base_ids is None:
        if run.get("items") != baseline.get("items"):
            return (
                f"run covers {run.get('items')} item(s), the baseline "
                f"{baseline.get('items')} — different datasets are not comparable"
            )
        return ""
    missing = sorted(set(base_ids) - set(run_ids))
    extra = sorted(set(run_ids) - set(base_ids))
    if not missing and not extra:
        return ""
    parts = []
    if missing:
        parts.append(f"{len(missing)} baseline item(s) absent from the run ({_sample(missing)})")
    if extra:
        parts.append(f"{len(extra)} run item(s) absent from the baseline ({_sample(extra)})")
    return (
        "run and baseline measure different item sets — " + "; ".join(parts) + ". "
        "Gate a run of the baseline's dataset, or refresh the baseline alongside it."
    )


def _too_few_trials(run: dict[str, Any], min_trials: int) -> str:
    trials = run.get("trials") or 0
    if trials >= min_trials:
        return ""
    return (
        f"run has {trials} trial(s); the gate needs k>={min_trials} to verdict "
        "(certification measured a 20% single-trial flip rate — a k=1 diff on "
        "marginal items reads noise, #137)"
    )


def _worker_mismatch(run: dict[str, Any], baseline: dict[str, Any]) -> str:
    run_model, base_model = run.get("model"), baseline.get("model")
    if run_model and base_model and run_model != base_model:
        return (
            f"run worker {run_model!r} differs from the baseline's {base_model!r} — "
            "compare a different worker with `arms`, not against this baseline"
        )
    return ""


def _new_slice_warning(name: str) -> str:
    base = f"slice {name!r} is new (no baseline to compare)"
    if name in PROTECTED_SLICES:
        return f"{base} — this slice is unguarded until a baseline refresh includes it"
    return base


def _sample(ids: list[str], limit: int = 3) -> str:
    shown = ", ".join(ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, ..."


def _drop(current: float | None, base: float | None) -> float | None:
    if current is None or base is None:
        return None
    return base - current


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _slice_table(run: dict[str, Any], baseline: dict[str, Any], bar: float) -> list[str]:
    run_slices, base_slices = run.get("slices", {}), baseline.get("slices", {})
    names = sorted(set(run_slices) | set(base_slices))
    if not names:
        return []
    lines = ["", f"  {'slice':<26}{'baseline':>10}{'new':>9}{'delta':>9}   verdict"]
    for name in names:
        base, new = base_slices.get(name), run_slices.get(name)
        if base is None:
            lines.append(f"  {name:<26}{'—':>10}{new:>9.2f}{'—':>9}   NEW")
        elif new is None:
            lines.append(f"  {name:<26}{base:>10.2f}{'—':>9}{'—':>9}   ABSENT")
        else:
            drop = base - new
            mark = "BREACH" if drop > bar else "OK"
            lines.append(f"  {name:<26}{base:>10.2f}{new:>9.2f}{new - base:>+9.2f}   {mark}")
    return lines


def _headline(run: dict[str, Any], baseline: dict[str, Any], bar: float) -> list[str]:
    new, base = run.get("pass_all_trials"), baseline.get("pass_all_trials")
    if new is None or base is None:
        return []
    drop = base - new
    mark = "BREACH" if drop > bar else "OK"
    return [
        "",
        f"  overall pass^k  {base:.3f} -> {new:.3f}  ({new - base:+.3f})   {mark} (bar {bar:.2f})",
        f"  items {run.get('items', '?')}  trials {run.get('trials', '?')}  "
        f"fabrications {run.get('fabrication_count', 0)}",
    ]


def _failing(run: dict[str, Any], limit: int = 5) -> list[str]:
    items = run.get("failing_items") or []
    if not items:
        return []
    lines = ["", f"  failing items ({len(items)}):"]
    for item in items[:limit]:
        lines.append(f"    {item['id']:<28} {', '.join(item['dims']) or 'pass^k only'}")
    if len(items) > limit:
        lines.append(f"    ... and {len(items) - limit} more")
    return lines


def render_verdict(verdict: GateVerdict) -> str:
    """The comment a reviewer acts on without leaving the PR (#152):
    the headline with its delta, every slice marked OK/BREACH, and the
    items that failed with the dimension that failed them."""
    if verdict.undecided:
        lines = [
            "EVAL GATE: UNDECIDED — run cannot be verdicted",
            f"  ✗ {verdict.undecided_reason}",
        ]
    elif verdict.passed:
        lines = ["EVAL GATE: PASS"]
    else:
        lines = ["EVAL GATE: BLOCKED"]
    for b in verdict.blocks:
        lines.append(f"  ✗ {b}")
    for w in verdict.warnings:
        lines.append(f"  ! {w}")
    if verdict.run and verdict.baseline and not verdict.undecided:
        lines += _headline(verdict.run, verdict.baseline, verdict.overall_drop)
        lines += _slice_table(verdict.run, verdict.baseline, verdict.slice_drop)
        lines += _failing(verdict.run)
    return "\n".join(lines)


def verdict_status(verdict: GateVerdict) -> tuple[str, str]:
    if verdict.undecided:
        return "UNDECIDED", "⚠️"
    return ("PASS", "✅") if verdict.passed else ("BLOCK", "❌")


def render_verdict_md(verdict: GateVerdict, *, note: str = "") -> str:
    """A titled Markdown comment so the gate reads like the other PR
    diagnostics: a status heading, the selection note, and the full
    text detail folded into a monospace block that keeps the table."""
    label, emoji = verdict_status(verdict)
    parts = [f"## {emoji} Eval gate — {label}", ""]
    if note:
        parts += [note, ""]
    parts += [
        "<details><summary>details</summary>",
        "",
        "```",
        render_verdict(verdict),
        "```",
        "",
        "</details>",
    ]
    return "\n".join(parts)


def render_skip_md(message: str) -> str:
    return f"## ⏭️ Eval gate — skipped\n\n{message}"
