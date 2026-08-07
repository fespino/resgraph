"""D28's privileged capability — `apply_remediation`.

Nothing here writes to a store. An approved step becomes a D2 update
emitted onto the same ingest stream every other producer uses, applied
by the same idempotent path (D3/D10). The watermark drops a stale
message silently, so every step confirms: emit, read the watermark
back, and fail the step if it did not land. A remediation that
vanished is a failure, never a success nobody checked.

Two properties fall out of D2 and matter more than they look:

- An upsert is a full statement — it replaces the attribute bag and
  the owned edge set. A patch is therefore merged onto the target's
  *live* state at apply time, never onto the render-time snapshot,
  or remediation would quietly revert whatever changed in between.
- The rollback target is the snapshot the approver saw. If the
  resource moved past the sequence this step wrote, that snapshot no
  longer restores anything and the step reports itself irreversible
  rather than clobbering a third party's write.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from resgraph.graph.ingest import read_node
from resgraph.schema import Op, Relationship, ResourceType, UpdateMessage
from resgraph.tools.context import CallerContext

from .remediation import ROLLBACK_IRREVERSIBLE, PlannedStep, StepEvent, StepMachine

SET_ATTRS = "set_attrs"
DELETE = "delete"
ACTIONS = (SET_ATTRS, DELETE)

WRITE_SCOPE = "resgraph:write"


class ApplyRemediationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    steps: list[PlannedStep] = Field(min_length=1)


class ApplyRemediationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    events: list[StepEvent]
    summary: dict[str, str]


def _authorize(ctx: CallerContext) -> Callable[[list[UpdateMessage]], None]:
    """The capability is the injected write channel, not a flag: a
    context without one cannot execute, whatever it claims to be."""
    if ctx.caller != "operator":
        raise PermissionError(
            f"apply_remediation is operator-only; caller was {ctx.caller!r} "
            "(the agent's identity can never reach this code — D26)"
        )
    if WRITE_SCOPE not in ctx.scopes:
        raise PermissionError(f"apply_remediation needs the {WRITE_SCOPE!r} scope")
    if ctx.emit is None:
        raise PermissionError("apply_remediation has no write channel to the ingest stream")
    return ctx.emit


def _rel_models(rels: list[tuple[str, str]]) -> list[Relationship]:
    return [Relationship(type=t.lower(), target_id=tid) for t, tid in rels]  # type: ignore[arg-type]


@dataclass
class Remediator:
    """The apply/rollback pair the step machine drives. Injectable end
    to end so the contract is testable without a store or a stream."""

    read: Callable[[str], dict[str, Any] | None]
    emit: Callable[[list[UpdateMessage]], None]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    confirm_attempts: int = 40
    confirm_delay_s: float = 0.05
    # keyed on step identity: the machine hands the same objects to
    # rollback, and two steps can legitimately target one resource
    _written: dict[int, int] = field(default_factory=dict)

    def _state(self, target: str) -> dict[str, Any] | None:
        return self.read(target)

    def _emit_and_confirm(self, msg: UpdateMessage) -> int:
        self.emit([msg])
        for attempt in range(self.confirm_attempts):
            state = self._state(msg.resource_id)
            if state is not None and (state.get("applied_seq") or -1) >= msg.sequence:
                return msg.sequence
            if attempt + 1 < self.confirm_attempts:
                self.sleep(self.confirm_delay_s)
        raise RuntimeError(
            f"{msg.resource_id}: emitted sequence {msg.sequence} never reached the store "
            "(the watermark discarded it, or ingest is not running)"
        )

    def _message(
        self,
        *,
        target: str,
        sequence: int,
        op: Op,
        attrs: dict[str, Any] | None = None,
        rels: list[tuple[str, str]] | None = None,
    ) -> UpdateMessage:
        return UpdateMessage(
            event_time=self.now(),
            sequence=sequence,
            op=op,
            resource_type=ResourceType(target.split("-", 1)[0]),
            resource_id=target,
            attrs=dict(attrs or {}),
            relationships=_rel_models(rels or []),
        )

    def apply_step(self, step: PlannedStep) -> str:
        if step.action not in ACTIONS:
            raise ValueError(
                f"unknown remediation action {step.action!r}; expected one of {ACTIONS}"
            )
        live = self._state(step.target)
        if live is None:
            raise RuntimeError(f"{step.target}: not in the graph; nothing to remediate")
        sequence = (live.get("applied_seq") or -1) + 1
        if step.action == DELETE:
            msg = self._message(target=step.target, sequence=sequence, op=Op.DELETE)
        else:
            # upsert replaces the bag: merge onto live state, not onto
            # the render-time snapshot
            msg = self._message(
                target=step.target,
                sequence=sequence,
                op=Op.UPSERT,
                attrs={**live["attrs"], **step.patch},
                rels=live["rels"],
            )
        written = self._emit_and_confirm(msg)
        self._written[id(step)] = written
        return f"{step.target}@{written}"

    def rollback_step(self, step: PlannedStep) -> object:
        if step.pre_state is None:
            return ROLLBACK_IRREVERSIBLE
        written = self._written.get(id(step))
        live = self._state(step.target)
        if written is None or live is None or (live.get("applied_seq") or -1) != written:
            return ROLLBACK_IRREVERSIBLE
        snapshot = step.pre_state
        if snapshot.get("deleted"):
            msg = self._message(target=step.target, sequence=written + 1, op=Op.DELETE)
        else:
            msg = self._message(
                target=step.target,
                sequence=written + 1,
                op=Op.UPSERT,
                attrs=snapshot.get("attrs", {}),
                rels=[tuple(r) for r in snapshot.get("rels", [])],  # type: ignore[misc]
            )
        self._emit_and_confirm(msg)
        return f"{step.target}@{written + 1}"


def apply_remediation(args: ApplyRemediationIn, *, ctx: CallerContext) -> ApplyRemediationOut:
    """Execute an approved plan. Called by the operator path only; the
    agent's toolset refuses this registration by construction (D26)."""
    emit = _authorize(ctx)
    remediator = Remediator(read=capture_pre_state(ctx.query.require("hot")), emit=emit)
    machine = StepMachine(
        args.steps,
        run_id=args.run_id,
        owner=args.owner,
        apply=remediator.apply_step,
        rollback=remediator.rollback_step,
    )
    events = machine.execute()
    return ApplyRemediationOut(run_id=args.run_id, events=events, summary=machine.summary())


def capture_pre_state(session: Any) -> Callable[[str], dict[str, Any] | None]:
    """Pre-state capture for `remediation.render_plan`: the approver
    sees the live snapshot a rollback would restore."""

    def capture(target: str) -> dict[str, Any] | None:
        return read_node(session, target)

    return capture
