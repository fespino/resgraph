"""D28's privileged capability, end to end against a fake ingest.

The fake implements exactly the two D2/D3 rules the executor has to
respect — the watermark drops a message whose sequence is not ahead,
and an upsert replaces the attribute bag and the owned edge set — so a
regression in either shows up here rather than only against a store.
"""

import copy
from typing import Any

import pytest

from resgraph.analyst.executor import (
    ApplyRemediationIn,
    Remediator,
    apply_remediation,
)
from resgraph.analyst.remediation import ROLLBACK_IRREVERSIBLE, PlannedStep, StepMachine, StepStatus
from resgraph.query.executor import QueryContext
from resgraph.schema import Op
from resgraph.tools.context import CallerContext

VM = "vm-000001"
HOST = "host-000001"


def node(resource_id: str, seq: int, attrs: dict[str, Any], rels: list[tuple[str, str]]):
    return {
        "id": resource_id,
        "applied_seq": seq,
        "deleted": False,
        "deleted_seq": None,
        "phantom": False,
        "attrs": attrs,
        "rels": rels,
    }


class FakeWorld:
    """The ingest's contract, in a dict: watermark first, then D2's
    replace-the-whole-statement upsert."""

    def __init__(self, nodes: dict[str, dict[str, Any]], *, deliver: bool = True) -> None:
        self.nodes = nodes
        self.deliver = deliver
        self.emitted: list[Any] = []

    def read(self, target: str) -> dict[str, Any] | None:
        found = self.nodes.get(target)
        return copy.deepcopy(found) if found is not None else None

    def emit(self, msgs: list[Any]) -> None:
        self.emitted.extend(msgs)
        if not self.deliver:
            return
        for m in msgs:
            current = self.nodes.get(m.resource_id)
            if m.sequence <= (current or {}).get("applied_seq", -1):
                continue
            if m.op is Op.DELETE:
                updated = dict(current or node(m.resource_id, -1, {}, []))
                updated |= {"applied_seq": m.sequence, "deleted": True, "deleted_seq": m.sequence}
            else:
                updated = node(
                    m.resource_id,
                    m.sequence,
                    dict(m.attrs),
                    sorted((r.type.upper(), r.target_id) for r in m.relationships),
                )
            self.nodes[m.resource_id] = updated


def remediator(world: FakeWorld) -> Remediator:
    return Remediator(read=world.read, emit=world.emit, sleep=lambda _s: None)


def step(action: str, target: str, patch: dict[str, Any], pre: dict[str, Any] | None):
    return PlannedStep(action=action, target=target, patch=patch, pre_state=pre)


def test_set_attrs_merges_onto_live_state_and_keeps_edges():
    """The trap D2 sets: an upsert replaces the bag, so a patch that
    forgets the existing attrs and edges silently strips them."""
    world = FakeWorld({VM: node(VM, 7, {"role": "web", "az": "a"}, [("RUNS_ON", HOST)])})
    pre = world.read(VM)
    r = remediator(world)

    ref = r.apply_step(step("set_attrs", VM, {"role": "drained"}, pre))

    assert ref == f"{VM}@8"
    after = world.nodes[VM]
    assert after["attrs"] == {"role": "drained", "az": "a"}
    assert after["rels"] == [("RUNS_ON", HOST)]
    assert after["applied_seq"] == 8


def test_a_message_the_watermark_discards_fails_the_step():
    """An emit that never lands must not read as success."""
    world = FakeWorld({VM: node(VM, 7, {"role": "web"}, [])}, deliver=False)
    r = remediator(world)
    r.confirm_attempts = 2

    with pytest.raises(RuntimeError, match="never reached the store"):
        r.apply_step(step("set_attrs", VM, {"role": "drained"}, world.read(VM)))


def test_rollback_restores_the_snapshot_the_approver_saw():
    world = FakeWorld({VM: node(VM, 7, {"role": "web"}, [("RUNS_ON", HOST)])})
    pre = world.read(VM)
    r = remediator(world)
    s = step("set_attrs", VM, {"role": "drained"}, pre)

    r.apply_step(s)
    outcome = r.rollback_step(s)

    assert outcome is not ROLLBACK_IRREVERSIBLE
    assert world.nodes[VM]["attrs"] == {"role": "web"}
    assert world.nodes[VM]["rels"] == [("RUNS_ON", HOST)]


def test_rollback_is_irreversible_once_someone_else_has_written():
    world = FakeWorld({VM: node(VM, 7, {"role": "web"}, [])})
    pre = world.read(VM)
    r = remediator(world)
    s = step("set_attrs", VM, {"role": "drained"}, pre)
    r.apply_step(s)

    # a third party moves the resource past our write
    world.nodes[VM] = node(VM, 99, {"role": "someone-elses-value"}, [])

    assert r.rollback_step(s) is ROLLBACK_IRREVERSIBLE
    assert world.nodes[VM]["attrs"] == {"role": "someone-elses-value"}


def test_delete_tombstones_and_rollback_revives():
    world = FakeWorld({VM: node(VM, 7, {"role": "web"}, [])})
    pre = world.read(VM)
    r = remediator(world)
    s = step("delete", VM, {}, pre)

    r.apply_step(s)
    assert world.nodes[VM]["deleted"] is True

    r.rollback_step(s)
    assert world.nodes[VM]["deleted"] is False
    assert world.nodes[VM]["attrs"] == {"role": "web"}


def test_a_step_with_no_pre_state_is_irreversible_not_guessed():
    world = FakeWorld({VM: node(VM, 7, {"role": "web"}, [])})
    r = remediator(world)
    assert r.rollback_step(step("set_attrs", VM, {"role": "x"}, None)) is ROLLBACK_IRREVERSIBLE


def test_unknown_action_fails_the_step_rather_than_inventing_one():
    world = FakeWorld({VM: node(VM, 7, {}, [])})
    with pytest.raises(ValueError, match="unknown remediation action"):
        remediator(world).apply_step(step("reboot", VM, {}, world.read(VM)))


def test_a_missing_target_fails_before_emitting_anything():
    world = FakeWorld({})
    with pytest.raises(RuntimeError, match="nothing to remediate"):
        remediator(world).apply_step(step("set_attrs", VM, {"role": "x"}, None))
    assert world.emitted == []


def test_failure_mid_plan_unwinds_the_earlier_step():
    world = FakeWorld(
        {
            VM: node(VM, 7, {"role": "web"}, []),
            HOST: node(HOST, 3, {"state": "up"}, []),
        }
    )
    r = remediator(world)
    plan = [
        step("set_attrs", VM, {"role": "drained"}, world.read(VM)),
        step("reboot", HOST, {}, world.read(HOST)),
    ]
    machine = StepMachine(
        plan, run_id="r1", owner="ops", apply=r.apply_step, rollback=r.rollback_step
    )

    machine.execute()

    assert machine.summary() == {"step_0": StepStatus.ROLLED_BACK, "step_1": StepStatus.FAILED}
    assert world.nodes[VM]["attrs"] == {"role": "web"}


def _ctx(caller: str, world: FakeWorld, *, scopes: frozenset[str], emit: bool = True):
    return CallerContext(
        caller=caller,  # type: ignore[arg-type]
        scopes=scopes,
        query=QueryContext(),
        emit=world.emit if emit else None,
    )


def test_the_agents_identity_can_never_execute():
    world = FakeWorld({VM: node(VM, 7, {}, [])})
    args = ApplyRemediationIn(
        run_id="r1", owner="ops", steps=[step("set_attrs", VM, {"a": "b"}, world.read(VM))]
    )
    with pytest.raises(PermissionError, match="operator-only"):
        apply_remediation(args, ctx=_ctx("analyst", world, scopes=frozenset({"resgraph:write"})))


def test_an_operator_without_a_write_channel_cannot_execute():
    world = FakeWorld({VM: node(VM, 7, {}, [])})
    args = ApplyRemediationIn(
        run_id="r1", owner="ops", steps=[step("set_attrs", VM, {"a": "b"}, world.read(VM))]
    )
    with pytest.raises(PermissionError, match="no write channel"):
        apply_remediation(
            args, ctx=_ctx("operator", world, scopes=frozenset({"resgraph:write"}), emit=False)
        )


def test_an_operator_without_the_write_scope_cannot_execute():
    world = FakeWorld({VM: node(VM, 7, {}, [])})
    args = ApplyRemediationIn(
        run_id="r1", owner="ops", steps=[step("set_attrs", VM, {"a": "b"}, world.read(VM))]
    )
    with pytest.raises(PermissionError, match="resgraph:write"):
        apply_remediation(args, ctx=_ctx("operator", world, scopes=frozenset({"resgraph:read"})))
