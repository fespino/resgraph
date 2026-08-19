"""D26 — the approval gate between plan and execution.

The gate runs outside the agent loop: the model proposes a plan and
stops (D28 — the privileged tool is not in its tool blocks), and a
human sees the rendered plan — every irreversible step declared —
before anything mutates. Approval is typed, not clicked: the approver
types the count of steps being applied, so a plan that grew a step
since they last looked cannot be waved through on reflex. `skip N`
drops a step (renumbering nothing — step numbers are stable so a
skip can be discussed by number afterwards); `no` rejects.

The decision is itself an audit record: approver, plan hash, which
steps, and time-to-decision. A 900ms approval of a five-step plan is
a reviewable fact, not a lost keystroke.

Decisions: D26 (SPEC.md).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .remediation import PlannedStep


def plan_hash(plan: list[PlannedStep]) -> str:
    payload = json.dumps([s.model_dump() for s in plan], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def render_plan_text(plan: list[PlannedStep], *, skipped: frozenset[int] = frozenset()) -> str:
    """The text the approver decides on. Step numbers are 1-based and
    stable across skips; an irreversible step says so on its own line,
    in the render, before any decision."""
    lines = [f"Proposed remediation (plan {plan_hash(plan)[:8]}, {len(plan)} steps):"]
    for i, step in enumerate(plan):
        mark = "  [SKIPPED]" if i in skipped else ""
        if step.pre_state is None:
            detail = "[pre-state UNAVAILABLE → step is IRREVERSIBLE]"
        else:
            detail = (
                f"[current: {json.dumps(step.pre_state, sort_keys=True)}]"
                f" → [patch: {json.dumps(step.patch, sort_keys=True)}]"
            )
        lines.append(f"  {i + 1}. {step.target}  {step.action}  {detail}{mark}")
    return "\n".join(lines)


PROMPT = "Approve? Type the step count to apply, 'skip N' to drop a step, or 'no': "


@dataclass(frozen=True)
class ApprovalDecision:
    approver: str
    approved: bool
    plan_hash: str
    applied: tuple[int, ...]  # 0-based indices into the original plan
    skipped: tuple[int, ...]
    presented_at: datetime
    decided_at: datetime
    expires_at: datetime | None = None

    @property
    def time_to_decision_ms(self) -> int:
        return int((self.decided_at - self.presented_at).total_seconds() * 1000)


def approve_plan(
    plan: list[PlannedStep],
    *,
    approver: str,
    ask: Callable[[str], str],
    echo: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ttl_s: float | None = None,
) -> ApprovalDecision:
    """Interactive gate. `ask`/`echo`/`now` are injected so the gate's
    invariants are unit-testable; the CLI passes input/print."""
    digest = plan_hash(plan)
    presented = now()
    skipped: set[int] = set()

    def decide(approved: bool) -> ApprovalDecision:
        applied = tuple(i for i in range(len(plan)) if i not in skipped) if approved else ()
        decided = now()
        return ApprovalDecision(
            approver=approver,
            approved=approved,
            plan_hash=digest,
            applied=applied,
            skipped=tuple(sorted(skipped)),
            presented_at=presented,
            decided_at=decided,
            # ttl 0 means expired on arrival, not "no expiry"
            expires_at=(decided + timedelta(seconds=ttl_s)) if ttl_s is not None else None,
        )

    echo(render_plan_text(plan))
    while True:
        raw = ask(PROMPT).strip().lower()
        if raw == "no":
            return decide(approved=False)
        if raw.startswith("skip "):
            tail = raw.removeprefix("skip ").strip()
            if not tail.isdigit() or not 1 <= int(tail) <= len(plan):
                echo(f"No step {tail!r} — steps are 1..{len(plan)}.")
                continue
            skipped.add(int(tail) - 1)
            if len(skipped) == len(plan):
                echo("Every step skipped — rejecting the plan.")
                return decide(approved=False)
            echo(render_plan_text(plan, skipped=frozenset(skipped)))
            continue
        remaining = len(plan) - len(skipped)
        if raw.isdigit() and int(raw) == remaining:
            return decide(approved=True)
        if raw.isdigit():
            echo(f"That would apply {remaining} steps, not {raw} — count them and retype.")
            continue
        echo("Type the step count to apply, 'skip N', or 'no'.")
