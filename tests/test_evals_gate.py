"""D29b eval gate: fabrications block unconditionally, overall >2pp or
any slice >5pp block, the regression slice is named, and a sub-k=3 run
is declined rather than verdicted (#137)."""

from resgraph.evals.gate import evaluate

BASE = {
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
    assert any("k>=3" in b for b in v.blocks)


def test_new_slice_warns_not_blocks():
    v = evaluate(run(slices={**BASE["slices"], "budget_starved": 0.9}), BASE)
    assert v.passed
    assert any("budget_starved" in w and "new" in w for w in v.warnings)


def test_vanished_slice_warns():
    slimmer = {k: v for k, v in BASE["slices"].items() if k != "control"}
    v = evaluate(run(slices=slimmer), BASE)
    assert v.passed
    assert any("control" in w and "absent" in w for w in v.warnings)
