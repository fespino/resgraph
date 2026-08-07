"""The `resgraph-analyst triage` journey (#145): alert in, report out,
and — only when an operator asks for it — a rendered plan, a typed
approval, execution, and every stage on the audit trail.

The agent and the stores are stubbed; what is under test is the wiring
between the stages, which is the part no unit test of a stage can see.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from resgraph.analyst.audit import AuditStore
from resgraph.analyst.cli import app, parse_remediation
from resgraph.analyst.harness import RunResult, Usage
from resgraph.analyst.models import EvidenceVerdict, TriageReport, TriageSuspect
from resgraph.schema import Op

runner = CliRunner()

VM = "vm-000001"
HOST = "host-000001"


def _node(resource_id: str, seq: int, attrs: dict[str, Any]):
    return {
        "id": resource_id,
        "applied_seq": seq,
        "deleted": False,
        "deleted_seq": None,
        "phantom": False,
        "attrs": attrs,
        "rels": [("RUNS_ON", HOST)] if resource_id == VM else [],
    }


class World:
    def __init__(self) -> None:
        self.nodes = {VM: _node(VM, 7, {"role": "web"}), HOST: _node(HOST, 2, {"state": "up"})}
        self.emitted: list[Any] = []

    def read(self, _session: Any, target: str):
        found = self.nodes.get(target)
        return copy.deepcopy(found) if found is not None else None

    def emit(self, msgs: list[Any]) -> None:
        self.emitted.extend(msgs)
        for m in msgs:
            if m.sequence <= self.nodes.get(m.resource_id, {}).get("applied_seq", -1):
                continue
            node = _node(m.resource_id, m.sequence, dict(m.attrs))
            node["deleted"] = m.op is Op.DELETE
            self.nodes[m.resource_id] = node


class FakeSession:
    """Answers only the two shapes live_summary asks for."""

    def run(self, query: str, **params: Any):
        if "RETURN n.id AS id" in str(query):
            return [{"id": VM}, {"id": HOST}]
        return []

    def close(self) -> None:
        pass


def _report(suspects: list[str]) -> TriageReport:
    return TriageReport(
        suspects=[
            TriageSuspect(
                sequence=41,
                resource_id=s,
                mechanism_path=[s, VM],
                verdict=EvidenceVerdict(
                    mechanism_verified=True, event_found=True, explains_symptom=True
                ),
                confidence="high",
                evidence=["seq 41"],
            )
            for s in suspects
        ],
        no_confident_candidate=not suspects,
        narrative="the host went down and took the vm with it",
    )


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    world = World()
    monkeypatch.setattr(
        "resgraph.graph.client.get_driver",
        lambda: type(
            "D",
            (),
            {"verify_connectivity": lambda self: None, "session": lambda self: FakeSession()},
        )(),
    )
    monkeypatch.setattr("resgraph.graph.ingest.read_node", world.read)
    monkeypatch.setattr("resgraph.analyst.executor.read_node", world.read)
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: object())
    monkeypatch.setattr("resgraph.analyst.tools.default_toolset", lambda: object())
    monkeypatch.setattr(
        "resgraph.gen.sinks.RedisSink",
        lambda *a, **k: type(
            "S", (), {"emit_many": staticmethod(world.emit), "close": lambda self: None}
        )(),
    )

    def fake_run_triage(prompt, toolset, client, **kw):
        return RunResult(
            report=_report([HOST]), degraded=False, tool_calls=3, turns=2, usage=Usage()
        )

    monkeypatch.setattr("resgraph.analyst.harness.run_triage", fake_run_triage)
    return world, tmp_path / "audit.db"


def _events(db: Path, output: str) -> list[dict[str, Any]]:
    run_id = re.search(r"^run (\S+):", output, re.MULTILINE).group(1)  # type: ignore[union-attr]
    store = AuditStore(db)
    try:
        return store.timeline(run_id)
    finally:
        store.close()


def test_report_only_run_writes_nothing(wired):
    world, db = wired
    result = runner.invoke(app, ["triage", VM, "--symptom", "crash_loop", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "report only" in result.output
    assert HOST in result.output
    assert world.emitted == []


def test_remediation_without_an_approver_is_refused(wired):
    _world, db = wired
    result = runner.invoke(
        app, ["triage", VM, "--db", str(db), "--remediate", "set_attrs:state=drained"]
    )
    assert result.exit_code != 0
    assert "needs --approver" in result.output


def test_approved_plan_applies_to_the_agents_top_suspect(wired):
    world, db = wired
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--db",
            str(db),
            "--approver",
            "fran",
            "--remediate",
            "set_attrs:state=drained",
        ],
        input="1\n",
    )
    assert result.exit_code == 0, result.output
    # the step targeted the suspect the agent named, not the alerting resource
    assert world.nodes[HOST]["attrs"] == {"state": "drained"}
    assert world.nodes[VM]["attrs"] == {"role": "web"}
    kinds = [e["kind"] for e in _events(db, result.output)]
    assert "approval" in kinds and "step" in kinds


def test_a_rejected_plan_applies_nothing_but_is_still_recorded(wired):
    world, db = wired
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--db",
            str(db),
            "--approver",
            "fran",
            "--remediate",
            "set_attrs:state=drained",
        ],
        input="no\n",
    )
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    assert world.emitted == []
    events = _events(db, result.output)
    approval = [e for e in events if e["kind"] == "approval"]
    assert approval and approval[0]["payload"]["approved"] is False


def test_an_explicit_target_overrides_the_suspect(wired):
    world, db = wired
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--db",
            str(db),
            "--approver",
            "fran",
            "--remediate",
            f"set_attrs@{VM}:role=drained",
        ],
        input="1\n",
    )
    assert result.exit_code == 0, result.output
    assert world.nodes[VM]["attrs"] == {"role": "drained"}


def test_no_suspect_means_no_plan_is_proposed(wired, monkeypatch):
    world, db = wired
    monkeypatch.setattr(
        "resgraph.analyst.harness.run_triage",
        lambda *a, **k: RunResult(
            report=_report([]), degraded=False, tool_calls=1, turns=1, usage=Usage()
        ),
    )
    result = runner.invoke(
        app,
        ["triage", VM, "--db", str(db), "--approver", "fran", "--remediate", "set_attrs:state=x"],
        input="1\n",
    )
    assert result.exit_code == 1
    assert "refusing to propose a plan against nothing" in result.output
    assert world.emitted == []


def test_the_plan_the_approver_sees_declares_the_rollback_state(wired):
    _world, db = wired
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--db",
            str(db),
            "--approver",
            "fran",
            "--remediate",
            "set_attrs:state=drained",
        ],
        input="no\n",
    )
    assert "current:" in result.output
    assert json.dumps({"state": "drained"}, sort_keys=True) in result.output


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("set_attrs:state=drained", ("set_attrs", "top", {"state": "drained"})),
        ("delete", ("delete", "top", {})),
        (f"set_attrs@{VM}:a=1,b=true", ("set_attrs", VM, {"a": 1, "b": True})),
    ],
)
def test_parse_remediation(spec, expected):
    assert parse_remediation(spec, "top") == expected


def test_parse_remediation_rejects_a_malformed_patch():
    with pytest.raises(Exception, match="not k=v"):
        parse_remediation("set_attrs:state", "top")
