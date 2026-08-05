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


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Below the k>=3 flap floor: declined, not failed — CI treats it
    # differently from a block.
    undecided: bool = False


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
    trials = run.get("trials") or 0
    if trials < min_trials:
        return GateVerdict(
            passed=False,
            undecided=True,
            blocks=[
                f"run has {trials} trial(s); the gate needs k>={min_trials} to verdict "
                "(certification measured a 20% single-trial flip rate — a k=1 diff on "
                "marginal items reads noise, #137)"
            ],
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
            warnings.append(f"slice {name!r} is new (no baseline to compare)")
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

    return GateVerdict(passed=not blocks, blocks=blocks, warnings=warnings)


def _drop(current: float | None, base: float | None) -> float | None:
    if current is None or base is None:
        return None
    return base - current


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def render_verdict(verdict: GateVerdict) -> str:
    lines = []
    if verdict.undecided:
        lines.append("EVAL GATE: UNDECIDED — run cannot be verdicted")
    elif verdict.passed:
        lines.append("EVAL GATE: PASS")
    else:
        lines.append("EVAL GATE: BLOCKED")
    for b in verdict.blocks:
        lines.append(f"  ✗ {b}")
    for w in verdict.warnings:
        lines.append(f"  ! {w}")
    return "\n".join(lines)
