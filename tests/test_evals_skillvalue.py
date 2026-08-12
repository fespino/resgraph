"""The paired skill arm's ledger and its credit rule."""

from resgraph.evals.skillvalue import render, skill_invoked, skill_value


def _row(sid, *, passed, tools, trial=0):
    return {
        "scenario_id": sid,
        "scenario_type": "direct",
        "tags": ["direct"],
        "trial": trial,
        "dims": {
            "found_top3": {"passed": passed, "detail": ""},
            "evidence": {"passed": True, "detail": ""},
        },
        "tool_trace": [{"tool": t, "ok": True} for t in tools],
    }


INTERSECT = ["world_diff", "blast_radius", "resource_history"]
NO_INTERSECT = ["resource_history", "resource_history"]


def test_invoked_needs_the_intersect_signature():
    assert skill_invoked(_row("a", passed=True, tools=INTERSECT))
    assert not skill_invoked(_row("a", passed=True, tools=NO_INTERSECT))
    assert not skill_invoked(_row("a", passed=True, tools=["world_diff"]))


def test_a_win_where_the_skill_was_invoked_is_relevant():
    w = [_row("s1", passed=True, tools=INTERSECT)]
    wo = [_row("s1", passed=False, tools=NO_INTERSECT)]
    v = skill_value(w, wo)
    assert v["relevant"] == ["s1"] and v["invoked"] == 1


def test_a_win_without_invocation_is_not_credited():
    w = [_row("s1", passed=True, tools=NO_INTERSECT)]
    wo = [_row("s1", passed=False, tools=NO_INTERSECT)]
    v = skill_value(w, wo)
    assert v["relevant"] == [] and v["uncredited_wins"] == ["s1"]


def test_both_arms_passing_is_not_a_skill_win():
    w = [_row("s1", passed=True, tools=INTERSECT)]
    wo = [_row("s1", passed=True, tools=NO_INTERSECT)]
    v = skill_value(w, wo)
    assert v["both_pass"] == ["s1"] and v["relevant"] == []


def test_retrieved_collapses_into_available():
    w = [_row("s1", passed=True, tools=INTERSECT)]
    wo = [_row("s1", passed=True, tools=INTERSECT)]
    v = skill_value(w, wo)
    assert v["available"] == v["retrieved"] == 1


def test_invoked_is_a_majority_of_trials():
    w = [
        _row("s1", passed=True, tools=INTERSECT, trial=0),
        _row("s1", passed=True, tools=NO_INTERSECT, trial=1),
        _row("s1", passed=True, tools=INTERSECT, trial=2),
    ]
    wo = [_row("s1", passed=False, tools=NO_INTERSECT, trial=t) for t in range(3)]
    v = skill_value(w, wo)
    assert v["invoked"] == 1 and v["relevant"] == ["s1"]


def test_mismatched_item_sets_are_flagged():
    v = skill_value(
        [_row("a", passed=True, tools=INTERSECT)],
        [_row("b", passed=True, tools=INTERSECT)],
    )
    assert not v["comparable"]


def test_render_shows_the_ledger_and_the_collapse_note():
    v = skill_value(
        [_row("s1", passed=True, tools=INTERSECT)],
        [_row("s1", passed=False, tools=NO_INTERSECT)],
    )
    out = render(v)
    assert "available" in out and "retrieved" in out and "== available" in out
    assert "relevant" in out and "invoked" in out
