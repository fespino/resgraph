"""D26 approval-gate invariants: the typed count is the gate, skips
keep stable numbering, and the decision is an audit record."""

from datetime import UTC, datetime, timedelta

from resgraph.analyst.approval import (
    approve_plan,
    plan_hash,
    render_plan_text,
)
from resgraph.analyst.remediation import PlannedStep


def plan(n=3, irreversible=()):
    return [
        PlannedStep(
            action=f"a{i}",
            target=f"t{i}",
            patch={"k": i},
            pre_state=None if i in irreversible else {"k": "old"},
        )
        for i in range(n)
    ]


def asker(*answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def ticking(*seconds):
    base = datetime(2026, 8, 5, tzinfo=UTC)
    it = iter(seconds)
    return lambda: base + timedelta(seconds=next(it))


def gate(p, *answers, now=None):
    echoed = []
    decision = approve_plan(
        p,
        approver="fran",
        ask=asker(*answers),
        echo=echoed.append,
        now=now or ticking(0, 1),
    )
    return decision, echoed


def test_typed_count_must_match_remaining_steps():
    decision, echoed = gate(plan(), "2", "3")
    assert decision.approved and decision.applied == (0, 1, 2)
    assert any("3 steps, not 2" in line for line in echoed)


def test_skip_updates_the_count_and_keeps_numbering():
    decision, echoed = gate(plan(), "skip 2", "2")
    assert decision.approved
    assert decision.applied == (0, 2) and decision.skipped == (1,)
    rerender = [line for line in echoed if "[SKIPPED]" in line]
    assert rerender and "2. t1" in rerender[0]  # original number retained


def test_no_rejects_with_nothing_applied():
    decision, _ = gate(plan(), "no")
    assert not decision.approved and decision.applied == ()


def test_skipping_every_step_rejects():
    decision, _ = gate(plan(2), "skip 1", "skip 2")
    assert not decision.approved and decision.skipped == (0, 1)


def test_render_declares_irreversible_before_any_decision():
    text = render_plan_text(plan(3, irreversible={1}))
    assert "IRREVERSIBLE" in text.splitlines()[2]
    assert "pre-state UNAVAILABLE" in text


def test_decision_is_an_audit_record():
    decision, _ = gate(plan(), "3", now=ticking(0, 0.9))
    assert decision.approver == "fran"
    assert decision.plan_hash == plan_hash(plan())
    assert decision.time_to_decision_ms == 900


def test_plan_hash_tracks_content():
    changed = plan()
    changed[1] = PlannedStep(action="a1", target="t1", patch={"k": 99}, pre_state={"k": "old"})
    assert plan_hash(plan()) != plan_hash(changed)


def test_out_of_range_skip_reasks():
    decision, echoed = gate(plan(), "skip 9", "3")
    assert decision.approved and decision.skipped == ()
    assert any("No step" in line for line in echoed)
