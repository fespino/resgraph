"""Cross-arm cost analysis."""

from resgraph.evals.arms import arm_summary, compare, render


def _row(sid, model, passed, tokens_total, fab=False):
    dims = {
        "found_top3": {"passed": passed, "detail": ""},
        "evidence": {"passed": not fab, "detail": ""},
    }
    return {
        "scenario_id": sid,
        "scenario_type": "direct",
        "tags": ["direct"],
        "dims": dims,
        "tokens": {
            "input": 0,
            "output": tokens_total,
            "cache_read": 0,
            "cache_creation": 0,
            "total": tokens_total,
        },
        "model": model,
    }


def _arm(label, model, passes):
    rows = [_row(f"d{i}", model, p, 100_000) for i, p in enumerate(passes)]
    return arm_summary(label, rows)


def test_arm_summary_costs_per_passed_triage():
    # 2 of 4 items pass; 4 items * 100k output tokens each
    arm = _arm("opus", "claude-opus-4-8", [True, True, False, False])
    assert arm["items"] == 4 and arm["passed_items"] == 2
    assert arm["worker_cost"] > 0
    # cost per passed = total worker cost / passed items
    assert abs(arm["cost_per_passed"] - arm["worker_cost"] / 2) < 1e-9


def test_cheaper_model_lowers_cost_per_passed_at_equal_quality():
    opus = _arm("opus", "claude-opus-4-8", [True, True, True, False])
    sonnet = _arm("sonnet", "claude-sonnet-4-6", [True, True, True, False])
    assert sonnet["cost_per_passed"] < opus["cost_per_passed"]


def test_compare_reports_pass_k_delta_against_baseline():
    opus = _arm("opus", "claude-opus-4-8", [True, True, True, False])
    sonnet = _arm("sonnet", "claude-sonnet-4-6", [True, True, False, False])
    comp = compare([opus, sonnet], "opus")
    assert comp["comparable"]
    assert abs(comp["deltas"]["sonnet"] - (0.5 - 0.75)) < 1e-9


def test_compare_declines_on_mismatched_item_sets():
    opus = arm_summary("opus", [_row("a", "claude-opus-4-8", True, 100_000)])
    sonnet = arm_summary("sonnet", [_row("b", "claude-sonnet-4-6", True, 100_000)])
    comp = compare([opus, sonnet], "opus")
    assert not comp["comparable"] and "sonnet" in comp["mismatched"]
    assert "DECLINED" in render([opus, sonnet], comp)


def test_render_shows_the_tier_table():
    opus = _arm("opus", "claude-opus-4-8", [True, False])
    out = render([opus], compare([opus], "opus"))
    assert "$/passed" in out and "opus" in out
