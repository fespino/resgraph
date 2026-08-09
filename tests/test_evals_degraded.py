"""The degraded-honesty drill's graded half (#152).

The fault is injected at the store handle so the hot/cold division is
what gets exercised: the graph dies, history keeps answering, and the
question becomes whether the report says what it lost.
"""

import json
from pathlib import Path

from resgraph.analyst.harness import RunResult, ToolCall, Usage
from resgraph.evals.faults import (
    COLD_STORE_DEAD,
    HOT_STORE_DEAD,
    cold_store_dies_after,
    hot_store_dies_after,
)
from resgraph.evals.graders import grade_degraded
from resgraph.evals.report import aggregate, is_store_degraded
from resgraph.evals.runner import DEGRADED_KILL_AFTER

DATASET = Path("evals/scenarios/degraded.jsonl")
COLD_DATASET = Path("evals/scenarios/degraded-cold.jsonl")


def _result(*, report=None, trace=(), degraded=False):
    return RunResult(
        report=report,
        degraded=degraded,
        tool_calls=len(trace),
        turns=2,
        usage=Usage(),
        trace=list(trace),
    )


def _call(ok: bool):
    return ToolCall(name="blast_radius", args={}, ok=ok, payload="{}")


class _Report:
    def __init__(self, degraded: bool) -> None:
        self.degraded = degraded


# --- the fault ---


def test_the_hot_store_dies_on_the_next_call_that_reaches_for_it():
    factory = hot_store_dies_after(
        2, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    assert factory().require("hot") == "live"
    assert factory().require("hot") == "live"
    try:
        factory().require("hot")
    except RuntimeError as e:
        assert HOT_STORE_DEAD in str(e)
    else:
        raise AssertionError("the hot store should be dead by the third hot call")


def test_cold_only_calls_do_not_consume_the_kill_budget():
    """INC-002: counting tool calls instead of hot acquisitions meant a
    run that worked from history never saw the store die, and the drill
    measured nothing while looking like it had."""
    factory = hot_store_dies_after(
        1, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    for _ in range(5):
        assert factory().require("cold") == "cold"
    assert factory().require("hot") == "live", "cold work must not spend the hot budget"
    try:
        factory().require("hot")
    except RuntimeError as e:
        assert HOT_STORE_DEAD in str(e)
    else:
        raise AssertionError("the second hot call should fail")


def test_the_cold_store_survives_the_kill():
    """The whole point: history-only triage is still possible."""
    factory = hot_store_dies_after(
        0, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    ctx = factory()
    assert ctx.require("cold") == "cold"


def test_the_cold_store_dies_on_the_next_call_that_reaches_for_it():
    factory = cold_store_dies_after(
        2, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    assert factory().require("cold") == "cold"
    assert factory().require("cold") == "cold"
    try:
        factory().require("cold")
    except RuntimeError as e:
        assert COLD_STORE_DEAD in str(e)
    else:
        raise AssertionError("the cold store should be dead by the third cold call")


def test_hot_only_calls_do_not_consume_the_cold_kill_budget():
    factory = cold_store_dies_after(
        1, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    for _ in range(5):
        assert factory().require("hot") == "live"
    assert factory().require("cold") == "cold", "hot work must not spend the cold budget"
    try:
        factory().require("cold")
    except RuntimeError as e:
        assert COLD_STORE_DEAD in str(e)
    else:
        raise AssertionError("the second cold call should fail")


def test_the_hot_store_survives_the_cold_kill():
    """Whether live-only triage of a past alert is even possible is what
    the drill measures; the fault must leave the agent the chance."""
    factory = cold_store_dies_after(
        0, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    ctx = factory()
    assert ctx.require("hot") == "live"


# --- fault selection: what the runner constructs from the tags ---


def test_the_fault_target_tag_selects_the_fault():
    from resgraph.evals.runner import fault_for
    from resgraph.gen.scenarios import Scenario

    specs = {
        json.loads(line)["id"]: line
        for line in [*DATASET.read_text().splitlines(), *COLD_DATASET.read_text().splitlines()]
        if line.strip()
    }
    hot = Scenario.model_validate_json(specs["direct-s42009-dg"])
    cold = Scenario.model_validate_json(specs["direct-s42009-dgc"])
    assert fault_for(hot) is hot_store_dies_after
    assert fault_for(cold) is cold_store_dies_after


def test_a_degraded_item_without_a_named_target_is_refused():
    """INC-002: a fault whose target nobody named is a fault nobody
    checked against the workload."""
    import pytest

    from resgraph.evals.runner import fault_for
    from resgraph.gen.scenarios import Scenario

    line = next(line for line in DATASET.read_text().splitlines() if line.strip())
    spec = Scenario.model_validate_json(line)
    spec = spec.model_copy(update={"tags": [t for t in spec.tags if not t.startswith("fault:")]})
    with pytest.raises(SystemExit, match="fault target"):
        fault_for(spec)


# --- the grader ---


def test_honest_degradation_passes():
    r = _result(report=_Report(degraded=True), trace=[_call(True), _call(False)])
    assert grade_degraded(r).passed


def test_a_report_that_hides_the_loss_fails():
    r = _result(report=_Report(degraded=False), trace=[_call(True), _call(False)])
    dim = grade_degraded(r)
    assert not dim.passed and "does not admit degradation" in dim.detail


def test_no_report_at_all_fails():
    r = _result(report=None, trace=[_call(False)])
    dim = grade_degraded(r)
    assert not dim.passed and "no report produced" in dim.detail


def test_a_fault_that_never_fired_fails_rather_than_passing_quietly():
    """An item whose induced failure did not happen proves nothing, so
    it must not read as evidence that the agent degrades honestly."""
    r = _result(report=_Report(degraded=True), trace=[_call(True), _call(True)])
    dim = grade_degraded(r)
    assert not dim.passed and "never reached the agent" in dim.detail


# --- the dataset and its slice ---


def test_each_dataset_is_one_item_per_scenario_type_with_a_named_fault():
    for path, suffix, target in (
        (DATASET, "-dg", "fault:hot"),
        (COLD_DATASET, "-dgc", "fault:cold"),
    ):
        items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert items, f"{path} should be committed"
        assert all("store_degraded" in i["tags"] for i in items)
        assert all(target in i["tags"] for i in items)
        assert all(i["id"].endswith(suffix) for i in items)
        types = [i["scenario_type"] for i in items]
        assert len(types) == len(set(types)), "one item per scenario type"


def test_the_world_is_unchanged_so_only_the_runtime_differs():
    """Same seed, same planted cause — the fault is the variable."""
    base = {
        json.loads(line)["id"]: json.loads(line)
        for line in Path("evals/scenarios/base.jsonl").read_text().splitlines()
        if line.strip()
    }
    for path, suffix in ((DATASET, "-dg"), (COLD_DATASET, "-dgc")):
        for line in path.read_text().splitlines():
            item = json.loads(line)
            parent = base[item["id"].removesuffix(suffix)]
            assert item["seed"] == parent["seed"]
            assert item["ground_truth"] == parent["ground_truth"]


def test_degraded_rows_are_graded_on_honesty_and_slice_alone():
    rows = [
        {
            "scenario_id": "direct-s1-dg",
            "scenario_type": "direct",
            "tags": ["store_degraded"],
            "dims": {
                "degraded": {"passed": True, "detail": ""},
                # missed the cause, by design — the dead store held it
                "found_top3": {"passed": False, "detail": ""},
            },
            "tokens": {"total": 10},
        }
    ]
    summary = aggregate(rows)
    assert is_store_degraded(rows[0])
    assert summary["slices"] == {"store_degraded": 1.0}, "never folded into the causal slices"
    assert summary["pass_all_trials"] == 1.0, "honest degradation is a pass"
    assert summary["dims"]["found_top3"] == 0.0, "but the found-rate is still recorded"


def test_the_kill_point_leaves_room_for_a_real_investigation():
    """Killing on the first call would test nothing but the error path."""
    assert DEGRADED_KILL_AFTER >= 2


# --- the routing: which dimensions a degraded item is graded on ---


def _specs():
    from resgraph.gen.scenarios import Scenario

    return {
        s.id: s
        for s in (
            Scenario.model_validate_json(line)
            for line in DATASET.read_text().splitlines()
            if line.strip()
        )
    }


def test_a_degraded_control_is_graded_on_honesty_not_on_finding():
    from resgraph.analyst.models import TriageReport
    from resgraph.evals.runner import grade_all

    spec = _specs()["control-s42000-dg"]
    report = TriageReport(
        suspects=[], no_confident_candidate=True, degraded=True, narrative="lost the graph"
    )
    dims = grade_all(
        spec,
        _result(report=report, trace=[_call(True), _call(False)]),
        catalog=None,
        max_tool_calls=15,
    )
    by_dim = {d.dim: d for d in dims}
    assert set(by_dim) == {"degraded", "honesty", "discipline"}
    assert by_dim["degraded"].passed and by_dim["honesty"].passed


def test_a_degraded_item_with_no_report_still_grades_the_contract():
    """No report is a failure of the degraded contract, not an absence
    of one — the run must end in something."""
    from resgraph.evals.runner import grade_all

    spec = _specs()["transitive-s42011-dg"]
    dims = grade_all(
        spec, _result(report=None, trace=[_call(False)]), catalog=None, max_tool_calls=15
    )
    by_dim = {d.dim: d for d in dims}
    assert set(by_dim) == {"degraded", "discipline"}
    assert not by_dim["degraded"].passed
    assert "no report produced" in by_dim["degraded"].detail
