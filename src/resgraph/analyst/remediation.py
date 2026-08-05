"""D28 — the privileged tool's execution protocol.

The model proposes; this module executes. `apply_remediation` is not
in the agent's tool blocks: the agent produces a plan and stops, the
plan is rendered for approval (irreversibility declared BEFORE the
approver decides), and execution runs through a step machine whose
events are progress surfaces — no raw arguments in events, the audit
store owns those.

Cancel is cooperative and checked between steps only: a step is one
gated apply, and interrupting mid-commit recreates the partial-state
bug D12 exists to prevent. Rollback is reverse-order, best-effort,
honest, with a three-way contract: return ROLLBACK_IRREVERSIBLE when
the world moved and the pre-state no longer restores anything, any
other return is success, raise is rolled_back_failed — and in every
case the chain continues; strict re-raise would leave later steps
silently unattempted, the worst of both. Cancel requests arriving
during the unwind are dropped: the unwind runs to completion.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StepStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLED_BACK_FAILED = "rolled_back_failed"
    IRREVERSIBLE = "irreversible"
    CANCELLED = "cancelled"


# Sentinel a rollback callable returns to report that the step turned
# out unrecoverable at rollback time — the honest middle between lying
# (plain return) and alarming (raise).
ROLLBACK_IRREVERSIBLE = object()

_TERMINAL_FORWARD = {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
_TERMINAL_ROLLBACK = {
    StepStatus.ROLLED_BACK,
    StepStatus.ROLLED_BACK_FAILED,
    StepStatus.IRREVERSIBLE,
}


class StepEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    action: str
    step_index: int = Field(ge=0)
    total_steps: int = Field(gt=0)
    status: StepStatus
    output_ref: str | None = None
    error: str | None = None
    timestamp: datetime

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.step_index >= self.total_steps:
            raise ValueError("step_index must be < total_steps")
        if self.status is StepStatus.ROLLED_BACK_FAILED and not self.error:
            raise ValueError("rolled_back_failed requires an error")
        return self


class PlannedStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    target: str
    patch: dict[str, Any]
    # None means pre-state capture failed: the step is irreversible,
    # and the approver sees that before approving (D28).
    pre_state: dict[str, Any] | None


def render_plan(
    steps: list[tuple[str, str, dict[str, Any]]],
    capture: Callable[[str], dict[str, Any] | None],
) -> list[PlannedStep]:
    """Annotate each (action, target, patch) with its captured
    pre-state; a failed capture marks the step irreversible now, at
    render time, not mid-execution."""
    planned = []
    for action, target, patch in steps:
        try:
            pre = capture(target)
        except Exception:
            pre = None
        planned.append(PlannedStep(action=action, target=target, patch=patch, pre_state=pre))
    return planned


class StepMachine:
    def __init__(
        self,
        plan: list[PlannedStep],
        *,
        run_id: str,
        owner: str,
        apply: Callable[[PlannedStep], str],
        rollback: Callable[[PlannedStep], object],
    ) -> None:
        self.plan = plan
        self.run_id = run_id
        self.owner = owner
        self._apply = apply
        self._rollback = rollback
        self.events: list[StepEvent] = []
        self._cancel_requested = False
        self._finished = False

    def cancel(self, requested_by: str) -> None:
        """Cooperative: takes effect at the next between-steps check.
        Idempotent; a cancel after completion is a recorded no-op."""
        if requested_by != self.owner:
            raise PermissionError(f"only {self.owner!r} may cancel run {self.run_id}")
        if self._finished:
            return
        self._cancel_requested = True

    def _emit(self, index: int, status: StepStatus, **kw: Any) -> None:
        per_index = [e.status for e in self.events if e.step_index == index]
        if status is StepStatus.STARTED and StepStatus.STARTED in per_index:
            raise RuntimeError(f"step {index}: second started event")
        if status in _TERMINAL_FORWARD and per_index and per_index[-1] in _TERMINAL_FORWARD:
            raise RuntimeError(f"step {index}: second terminal-forward event")
        if status in _TERMINAL_ROLLBACK and any(s in _TERMINAL_ROLLBACK for s in per_index):
            raise RuntimeError(f"step {index}: second terminal-rollback event")
        self.events.append(
            StepEvent(
                run_id=self.run_id,
                action=self.plan[index].action,
                step_index=index,
                total_steps=len(self.plan),
                status=status,
                timestamp=datetime.now(UTC),
                **kw,
            )
        )

    def _unwind(self, executed: list[int]) -> None:
        for index in reversed(executed):
            step = self.plan[index]
            if step.pre_state is None:
                self._emit(index, StepStatus.IRREVERSIBLE)
                continue
            try:
                outcome = self._rollback(step)
            except Exception as e:
                self._emit(index, StepStatus.ROLLED_BACK_FAILED, error=str(e))
                continue
            if outcome is ROLLBACK_IRREVERSIBLE:
                self._emit(index, StepStatus.IRREVERSIBLE)
            else:
                self._emit(index, StepStatus.ROLLED_BACK)

    def execute(self) -> list[StepEvent]:
        executed: list[int] = []
        try:
            for index in range(len(self.plan)):
                if self._cancel_requested:
                    self._emit(index, StepStatus.CANCELLED)
                    self._unwind(executed)
                    return self.events
                self._emit(index, StepStatus.STARTED)
                try:
                    ref = self._apply(self.plan[index])
                except Exception as e:
                    self._emit(index, StepStatus.FAILED, error=str(e))
                    self._unwind(executed)
                    return self.events
                self._emit(index, StepStatus.SUCCEEDED, output_ref=ref)
                executed.append(index)
            return self.events
        finally:
            self._finished = True

    def summary(self) -> dict[str, str]:
        """Final disposition per executed-or-touched step index — the
        cancel invariant's 'the summary says which' surface."""
        out: dict[int, str] = {}
        for e in self.events:
            out[e.step_index] = e.status.value
        return {f"step_{i}": s for i, s in sorted(out.items())}
