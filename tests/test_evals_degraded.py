"""The degraded-honesty drill's graded half (#152).

The fault is injected at the store handle so the hot/cold division is
what gets exercised: the graph dies, history keeps answering, and the
question becomes whether the report says what it lost.
"""

import json
from pathlib import Path

from resgraph.analyst.harness import RunResult, ToolCall, Usage
from resgraph.evals.faults import HOT_STORE_DEAD, hot_store_dies_after
from resgraph.evals.graders import grade_degraded
from resgraph.evals.report import aggregate, is_store_degraded
from resgraph.evals.runner import DEGRADED_KILL_AFTER

DATASET = Path("evals/scenarios/degraded.jsonl")


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


def test_the_hot_store_dies_after_the_configured_call():
    made = []
    factory = hot_store_dies_after(
        2, session_factory=lambda: made.append("session") or "live", catalog_factory=lambda: "cold"
    )
    assert factory().require("hot") == "live"
    assert factory().require("hot") == "live"
    third = factory()
    try:
        third.require("hot")
    except RuntimeError as e:
        assert HOT_STORE_DEAD in str(e)
    else:
        raise AssertionError("the hot store should be dead by the third call")


def test_the_cold_store_survives_the_kill():
    """The whole point: history-only triage is still possible."""
    factory = hot_store_dies_after(
        0, session_factory=lambda: "live", catalog_factory=lambda: "cold"
    )
    ctx = factory()
    assert ctx.require("cold") == "cold"


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
    assert not dim.passed and "never fired" in dim.detail


# --- the dataset and its slice ---


def test_the_dataset_is_one_item_per_scenario_type_tagged_for_the_fault():
    items = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    assert items, "the degraded companion set should be committed"
    assert all("store_degraded" in i["tags"] for i in items)
    assert all(i["id"].endswith("-dg") for i in items)
    types = [i["scenario_type"] for i in items]
    assert len(types) == len(set(types)), "one item per scenario type"


def test_the_world_is_unchanged_so_only_the_runtime_differs():
    """Same seed, same planted cause — the fault is the variable."""
    base = {
        json.loads(line)["id"]: json.loads(line)
        for line in Path("evals/scenarios/base.jsonl").read_text().splitlines()
        if line.strip()
    }
    for line in DATASET.read_text().splitlines():
        item = json.loads(line)
        parent = base[item["id"].removesuffix("-dg")]
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
