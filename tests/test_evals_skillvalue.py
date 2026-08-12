"""The paired skill arm's ledger and its credit rule."""

from resgraph.evals.skillvalue import render, skill_invoked, skill_value

# The playbook's method: a tight ~10-min window and a radius reconstructed at
# incident time. The anti-patterns it warns against: a whole-day diff, or a
# live radius that never travels back to the incident.
TIGHT = {"from_t": "2026-01-01T14:20:00+00:00", "to_t": "2026-01-01T14:35:00+00:00"}
WIDE = {"from_t": "2026-01-01T00:00:00+00:00", "to_t": "2026-01-02T00:00:00+00:00"}
AT = {"depth": 2, "at": "2026-01-01T14:35:00+00:00"}
LIVE = {"depth": 2, "at": None}

FOLLOWED = [("world_diff", TIGHT), ("blast_radius", AT), ("resource_history", {})]
WIDE_WINDOW = [("world_diff", WIDE), ("blast_radius", AT)]
LIVE_RADIUS = [("world_diff", TIGHT), ("blast_radius", LIVE)]
NO_METHOD = [("resource_history", {}), ("resource_history", {})]


def _row(sid, *, passed, calls, trial=0):
    return {
        "scenario_id": sid,
        "scenario_type": "direct",
        "tags": ["direct"],
        "trial": trial,
        "dims": {
            "found_top3": {"passed": passed, "detail": ""},
            "evidence": {"passed": True, "detail": ""},
        },
        "tool_trace": [{"tool": t, "ok": True, "args": a} for t, a in calls],
    }


def test_invoked_scores_the_method_not_tool_presence():
    assert skill_invoked(_row("a", passed=True, calls=FOLLOWED))
    # both tools appear, but the diff spans a whole day: not the method
    assert not skill_invoked(_row("a", passed=True, calls=WIDE_WINDOW))
    # tight window, but the radius never reconstructs at incident time
    assert not skill_invoked(_row("a", passed=True, calls=LIVE_RADIUS))
    assert not skill_invoked(_row("a", passed=True, calls=NO_METHOD))


def test_a_win_where_the_skill_was_invoked_is_relevant():
    w = [_row("s1", passed=True, calls=FOLLOWED)]
    wo = [_row("s1", passed=False, calls=NO_METHOD)]
    v = skill_value(w, wo)
    assert v["relevant"] == ["s1"] and v["invoked"] == 1


def test_a_win_without_invocation_is_not_credited():
    w = [_row("s1", passed=True, calls=NO_METHOD)]
    wo = [_row("s1", passed=False, calls=NO_METHOD)]
    v = skill_value(w, wo)
    assert v["relevant"] == [] and v["uncredited_wins"] == ["s1"]


def test_both_arms_passing_is_not_a_skill_win():
    w = [_row("s1", passed=True, calls=FOLLOWED)]
    wo = [_row("s1", passed=True, calls=NO_METHOD)]
    v = skill_value(w, wo)
    assert v["both_pass"] == ["s1"] and v["relevant"] == []


def test_retrieved_collapses_into_available():
    w = [_row("s1", passed=True, calls=FOLLOWED)]
    wo = [_row("s1", passed=True, calls=FOLLOWED)]
    v = skill_value(w, wo)
    assert v["available"] == v["retrieved"] == 1


def test_invoked_is_a_majority_of_trials():
    w = [
        _row("s1", passed=True, calls=FOLLOWED, trial=0),
        _row("s1", passed=True, calls=NO_METHOD, trial=1),
        _row("s1", passed=True, calls=FOLLOWED, trial=2),
    ]
    wo = [_row("s1", passed=False, calls=NO_METHOD, trial=t) for t in range(3)]
    v = skill_value(w, wo)
    assert v["invoked"] == 1 and v["relevant"] == ["s1"]


def test_mismatched_item_sets_are_flagged():
    v = skill_value(
        [_row("a", passed=True, calls=FOLLOWED)],
        [_row("b", passed=True, calls=FOLLOWED)],
    )
    assert not v["comparable"]


def test_render_shows_the_ledger_and_the_collapse_note():
    v = skill_value(
        [_row("s1", passed=True, calls=FOLLOWED)],
        [_row("s1", passed=False, calls=NO_METHOD)],
    )
    out = render(v)
    assert "available" in out and "retrieved" in out and "== available" in out
    assert "relevant" in out and "invoked" in out
