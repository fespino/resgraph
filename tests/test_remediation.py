"""D28 step-machine invariants: the five cancel properties, honest
rollback, event uniqueness, and irreversibility declared at render
time — before the approver decides."""

from datetime import UTC, datetime

import pytest

from resgraph.analyst.remediation import (
    PlannedStep,
    StepEvent,
    StepMachine,
    StepStatus,
    render_plan,
)


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


def machine(p=None, apply=None, rollback=None, owner="fran"):
    return StepMachine(
        p or plan(),
        run_id="r1",
        owner=owner,
        apply=apply or (lambda s: f"ref-{s.target}"),
        rollback=rollback or (lambda s: None),
    )


def statuses(m, index):
    return [e.status for e in m.events if e.step_index == index]


def test_happy_path_emits_started_then_succeeded_per_step():
    m = machine()
    m.execute()
    for i in range(3):
        assert statuses(m, i) == [StepStatus.STARTED, StepStatus.SUCCEEDED]


def test_failure_rolls_back_executed_steps_in_reverse_order():
    rolled = []

    def apply(s):
        if s.target == "t2":
            raise RuntimeError("boom")
        return "ref"

    m = machine(apply=apply, rollback=lambda s: rolled.append(s.target))
    m.execute()
    assert rolled == ["t1", "t0"]
    assert StepStatus.FAILED in statuses(m, 2)
    assert statuses(m, 0)[-1] == StepStatus.ROLLED_BACK


def test_failed_rollback_reports_error_and_chain_continues():
    def rollback(s):
        if s.target == "t1":
            raise RuntimeError("rollback broke")

    def apply(s):
        if s.target == "t2":
            raise RuntimeError("boom")
        return "ref"

    m = machine(apply=apply, rollback=rollback)
    m.execute()
    e1 = [e for e in m.events if e.step_index == 1][-1]
    assert e1.status is StepStatus.ROLLED_BACK_FAILED
    assert "rollback broke" in (e1.error or "")
    assert statuses(m, 0)[-1] == StepStatus.ROLLED_BACK  # continued past the failure


def test_render_declares_irreversibility_before_approval():
    def capture(target):
        if target == "t1":
            raise RuntimeError("no pre-state")
        return {"k": "old"}

    p = render_plan([("a0", "t0", {}), ("a1", "t1", {}), ("a2", "t2", {})], capture)
    assert p[1].pre_state is None and p[0].pre_state is not None


def test_cancel_bounded_latency_and_terminal():
    m = machine()

    def apply(s):
        if s.target == "t0":
            m.cancel("fran")  # arrives mid-run; takes effect between steps
        return "ref"

    m._apply = apply
    m.execute()
    assert statuses(m, 1) == [StepStatus.CANCELLED]  # step 1 never applied
    assert statuses(m, 0)[-1] == StepStatus.ROLLED_BACK  # terminal: executed unwound
    assert m.summary()["step_1"] == "cancelled"


def test_cancel_idempotent_and_stale_safe():
    m = machine()
    m.cancel("fran")
    m.cancel("fran")  # double-cancel: no-op
    m.execute()
    assert statuses(m, 0) == [StepStatus.CANCELLED]
    n = machine()
    n.execute()
    events = len(n.events)
    n.cancel("fran")  # after completion: no error, no event
    assert len(n.events) == events


def test_cancel_scoped_to_owner():
    m = machine()
    with pytest.raises(PermissionError):
        m.cancel("mallory")


def test_cancel_terminal_with_irreversible_step_named_in_summary():
    m = machine(p=plan(3, irreversible={0}))

    def apply(s):
        if s.target == "t0":
            m.cancel("fran")
        return "ref"

    m._apply = apply
    m.execute()
    assert m.summary()["step_0"] == "irreversible"


def test_event_invariants_reject_bad_shapes():
    kw = dict(run_id="r", action="a", total_steps=2, timestamp=datetime.now(UTC))
    with pytest.raises(ValueError, match="step_index"):
        StepEvent(step_index=2, status=StepStatus.STARTED, **kw)
    with pytest.raises(ValueError, match="rolled_back_failed"):
        StepEvent(step_index=0, status=StepStatus.ROLLED_BACK_FAILED, **kw)


def test_per_index_uniqueness_enforced():
    m = machine()
    m._emit(0, StepStatus.STARTED)
    with pytest.raises(RuntimeError, match="second started"):
        m._emit(0, StepStatus.STARTED)
    m._emit(0, StepStatus.SUCCEEDED)
    with pytest.raises(RuntimeError, match="terminal-forward"):
        m._emit(0, StepStatus.FAILED)
