"""D29b eval gate: fabrications block unconditionally, overall >2pp or
any slice >5pp block, the regression slice is named, and a run the gate
cannot verdict — below k=3 (#137), or measuring different items than the
baseline — is declined rather than compared anyway."""

import json
import re
from pathlib import Path

from resgraph.evals.gate import evaluate, render_verdict
from resgraph.evals.report import aggregate

# every key the gate reads out of a report.aggregate summary
GATE_KEYS = ("trials", "items", "item_ids", "pass_all_trials", "fabrication_count", "slices")

CERTIFIED_RUN = Path("evals/runs/20260803T221121Z.jsonl")
BASELINE = Path("evals/baseline.json")

IDS = [f"item-{i}" for i in range(30)]

BASE = {
    "items": len(IDS),
    "item_ids": IDS,
    "trials": 3,
    "pass_all_trials": 0.667,
    "fabrication_count": 0,
    "slices": {
        "decoy": 0.50,
        "control": 0.78,
        "source:planted": 0.79,
        "source:failure_derived": 0.50,
    },
}


def run(**over):
    r = {**BASE, "fabrications": []}
    r.update(over)
    return r


def test_matching_run_passes():
    v = evaluate(run(), BASE)
    assert v.passed and not v.blocks and not v.undecided


def test_overall_drop_over_2pp_blocks():
    v = evaluate(run(pass_all_trials=0.63), BASE)
    assert not v.passed
    assert any("overall pass^k" in b for b in v.blocks)


def test_overall_drop_within_2pp_passes():
    v = evaluate(run(pass_all_trials=0.65), BASE)
    assert v.passed


def test_slice_drop_over_5pp_blocks_even_when_overall_flat():
    v = evaluate(run(slices={**BASE["slices"], "decoy": 0.40}), BASE)
    assert not v.passed
    assert any("decoy" in b for b in v.blocks)


def test_regression_slice_named_distinctly():
    v = evaluate(run(slices={**BASE["slices"], "source:failure_derived": 0.30}), BASE)
    assert not v.passed
    assert any("regression slice" in b and "failure_derived" in b for b in v.blocks)


def test_fabrication_blocks_unconditionally():
    v = evaluate(run(fabrication_count=1, fabrications=["decoy-x: fake edge"]), BASE)
    assert not v.passed
    assert any("fabrication" in b and "unconditional" in b for b in v.blocks)


def test_sub_k3_run_is_declined_not_verdicted():
    v = evaluate(run(trials=1), BASE)
    assert not v.passed and v.undecided
    assert "k>=3" in v.undecided_reason
    assert not v.blocks


def test_new_slice_warns_not_blocks():
    v = evaluate(run(slices={**BASE["slices"], "novel": 0.9}), BASE)
    assert v.passed
    assert any("novel" in w and "new" in w for w in v.warnings)


def test_new_protected_slice_says_it_is_unguarded():
    v = evaluate(run(slices={**BASE["slices"], "budget_starved": 0.9}), BASE)
    assert v.passed
    assert any("budget_starved" in w and "unguarded" in w for w in v.warnings)


def test_vanished_slice_warns():
    slimmer = {k: v for k, v in BASE["slices"].items() if k != "control"}
    v = evaluate(run(slices=slimmer), BASE)
    assert v.passed
    assert any("control" in w and "absent" in w for w in v.warnings)


def test_a_smaller_dataset_is_declined_not_passed():
    """The fail-open this closes: a 7-item run of another dataset used to
    score PASS against the 30-item baseline it never measured."""
    other = run(items=7, item_ids=[f"starved-{i}" for i in range(7)], pass_all_trials=1.0)
    v = evaluate(other, BASE)
    assert not v.passed and v.undecided
    assert "different item sets" in v.undecided_reason


def test_a_harder_dataset_is_declined_not_blocked():
    other = run(items=6, item_ids=[f"regression-{i}" for i in range(6)], pass_all_trials=0.33)
    v = evaluate(other, BASE)
    assert v.undecided and not v.blocks


def test_one_added_item_is_declined():
    v = evaluate(run(items=31, item_ids=[*IDS, "item-new"]), BASE)
    assert v.undecided
    assert "1 run item(s) absent from the baseline" in v.undecided_reason


def test_item_counts_catch_mismatch_when_ids_are_absent():
    legacy_base = {k: v for k, v in BASE.items() if k != "item_ids"}
    v = evaluate(run(items=7, item_ids=None), legacy_base)
    assert v.undecided and "not comparable" in v.undecided_reason


def test_comparability_is_checked_before_the_flap_floor():
    v = evaluate(run(trials=1, items=6, item_ids=["a"]), BASE)
    assert "different item sets" in v.undecided_reason


def test_render_verdict_marks_each_state():
    assert "PASS" in render_verdict(evaluate(run(), BASE))
    assert "BLOCKED" in render_verdict(evaluate(run(pass_all_trials=0.1), BASE))
    declined = render_verdict(evaluate(run(trials=1), BASE))
    assert "UNDECIDED" in declined and "k>=3" in declined


def test_gate_reads_the_keys_aggregate_actually_emits():
    """The contract test: hand-written fixtures cannot catch a key
    rename in report.aggregate, which would silently make every
    comparison None-vs-None and pass everything."""
    summary = aggregate(
        [json.loads(ln) for ln in CERTIFIED_RUN.read_text().splitlines() if ln.strip()]
    )
    baseline = json.loads(BASELINE.read_text())
    for key in GATE_KEYS:
        assert key in summary, f"report.aggregate no longer emits {key!r}; the gate reads it"
        assert key in baseline, f"evals/baseline.json has no {key!r}; the gate reads it"


def test_the_certified_run_passes_its_own_baseline():
    """End to end over committed evidence: the run the baseline was
    aggregated from must verdict, and pass."""
    summary = aggregate(
        [json.loads(ln) for ln in CERTIFIED_RUN.read_text().splitlines() if ln.strip()]
    )
    v = evaluate(summary, json.loads(BASELINE.read_text()))
    assert not v.undecided, v.undecided_reason
    assert v.passed, v.blocks
    assert not v.warnings


def test_render_marks_each_slice_and_names_the_failing_items():
    """OK/BREACH per slice, deltas, failing items, truncation."""
    regressed = run(
        pass_all_trials=0.60,
        slices={**BASE["slices"], "decoy": 0.30},
        failing_items=[{"id": f"item-{i:02d}", "dims": ["evidence"]} for i in range(7)],
    )
    text = render_verdict(evaluate(regressed, BASE))
    assert "overall pass^k  0.667 -> 0.600  (-0.067)   BREACH (bar 0.02)" in text
    assert re.search(r"decoy\s+0\.50\s+0\.30\s+-0\.20\s+BREACH", text)
    assert re.search(r"control\s+0\.78\s+0\.78\s+\+0\.00\s+OK", text)
    assert "failing items (7):" in text
    assert "item-00" in text and "evidence" in text
    assert "... and 2 more" in text


def test_render_shows_new_and_absent_slices_as_rows():
    moved = run(slices={k: v for k, v in BASE["slices"].items() if k != "control"} | {"novel": 0.9})
    text = render_verdict(evaluate(moved, BASE))
    assert re.search(r"novel\s+—\s+0\.90\s+—\s+NEW", text)
    assert re.search(r"control\s+0\.78\s+—\s+—\s+ABSENT", text)


def test_a_pass_still_renders_the_table():
    text = render_verdict(evaluate(run(), BASE))
    assert text.startswith("EVAL GATE: PASS")
    assert "overall pass^k" in text and "OK" in text


def test_custom_bars_ride_the_verdict_into_the_rendering():
    """evaluate's bars reach the rendering (#159 review drift)."""
    lenient = evaluate(run(pass_all_trials=0.62), BASE, overall_drop=0.10)
    assert lenient.passed
    text = render_verdict(lenient)
    assert "(bar 0.10)" in text and "OK" in text


def test_an_undecided_verdict_renders_no_misleading_table():
    text = render_verdict(evaluate(run(trials=1), BASE))
    assert "UNDECIDED" in text
    assert "BREACH" not in text and "failing items" not in text
