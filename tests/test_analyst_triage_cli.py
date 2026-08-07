"""The `resgraph-analyst triage` journey (#145): alert in, report out,
and — only when an operator asks for it — a rendered plan, a typed
approval, execution, and every stage on the audit trail.

The journey takes its collaborators, so this tests the sequence of
stages rather than how the command wires them. One double, at the
outermost boundary we do not own: a driver session backed by an
in-memory graph that answers the Cypher shapes the code actually
sends, and applies the D2/D3 rules on emit (watermark first, then
replace-the-whole-statement upsert). Everything between the CLI and
that session — live_summary, read_node, plan rendering, the approval
gate, the step machine, the executor, the audit store — is the real
thing.
"""

import json
from datetime import datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from resgraph.analyst.audit import AuditStore
from resgraph.analyst.cli import TriageIO, app, live_summary, parse_remediation, triage_journey
from resgraph.analyst.harness import RunResult, Usage
from resgraph.analyst.models import EvidenceVerdict, TriageReport, TriageSuspect
from resgraph.schema import Op

runner = CliRunner()

VM = "vm-000001"
HOST = "host-000001"
FIRED = "2026-01-02T03:04:05+00:00"


class _Rows(list):
    def single(self):
        return self[0] if self else None


class Graph:
    """The store's property bag and edges, plus the two ingest rules the
    executor depends on."""

    def __init__(self) -> None:
        self.props: dict[str, dict[str, Any]] = {
            VM: {"id": VM, "applied_seq": 7, "deleted": False, "phantom": False, "role": "web"},
            HOST: {"id": HOST, "applied_seq": 2, "deleted": False, "phantom": False, "state": "up"},
        }
        self.rels: dict[str, list[tuple[str, str]]] = {VM: [("RUNS_ON", HOST)], HOST: []}
        self.emitted: list[Any] = []

    def emit(self, msgs: list[Any]) -> None:
        self.emitted.extend(msgs)
        for m in msgs:
            current = self.props.get(m.resource_id, {}).get("applied_seq", -1)
            if m.sequence <= current:  # D3 watermark: stale or replayed
                continue
            if m.op is Op.DELETE:
                self.props[m.resource_id] |= {
                    "applied_seq": m.sequence,
                    "deleted": True,
                    "deleted_seq": m.sequence,
                }
                continue
            self.props[m.resource_id] = {
                "id": m.resource_id,
                "applied_seq": m.sequence,
                "deleted": False,
                "phantom": False,
                **dict(m.attrs),  # D2 upsert replaces the bag
            }
            self.rels[m.resource_id] = sorted(
                (r.type.upper(), r.target_id) for r in m.relationships
            )

    def attrs(self, resource_id: str) -> dict[str, Any]:
        system = {"id", "applied_seq", "deleted", "deleted_seq", "phantom"}
        return {k: v for k, v in self.props[resource_id].items() if k not in system}


class FakeSession:
    """Answers the three query shapes the triage path sends."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    def run(self, query: str, **params: Any) -> _Rows:
        q = str(query)
        if "properties(n) AS props" in q:
            found = self.graph.props.get(params["id"])
            if found is None:
                return _Rows()
            rels = [{"type": t, "target": tid} for t, tid in self.graph.rels[params["id"]]]
            # OPTIONAL MATCH with no edges collects one null row
            return _Rows([{"props": dict(found), "rels": rels or [{"type": None, "target": None}]}])
        if "RETURN n.id AS id" in q:
            return _Rows(
                [{"id": rid} for rid, p in self.graph.props.items() if not p.get("deleted")]
            )
        if "type(r) AS t" in q:
            return _Rows(
                [
                    {"src": src, "t": t}
                    for src, edges in self.graph.rels.items()
                    for t, tid in edges
                    if tid == params["id"]
                ]
            )
        return _Rows()

    def close(self) -> None:
        pass


class Script:
    """Scripted operator answers; runs out loudly rather than hanging."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)

    def __call__(self, _prompt: str) -> str:
        assert self.answers, "the gate asked more questions than the test scripted"
        return self.answers.pop(0)


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


def _agent(report: TriageReport):
    def run(prompt, toolset, client, **kw):
        return RunResult(report=report, degraded=False, tool_calls=3, turns=2, usage=Usage())

    return run


@pytest.fixture()
def journey(tmp_path):
    """Returns (run_it, graph, store) — run_it drives the real journey."""
    graph = Graph()
    store = AuditStore(tmp_path / "audit.db")
    echoed: list[str] = []

    def run_it(*, suspects=(HOST,), remediate=(), answers=(), approver="fran"):
        code = triage_journey(
            resource_id=VM,
            symptom="crash_loop",
            fired_at=datetime.fromisoformat(FIRED),
            remediate=list(remediate),
            approver=approver,
            model="claude-test",
            window_h=24,
            run_id="RUN1",
            git_ref="abc1234",
            io=TriageIO(
                session=FakeSession(graph),
                client=object(),
                toolset=object(),
                store=store,
                run=_agent(_report(list(suspects))),
                emit=graph.emit,
                ask=Script(*answers),
                echo=echoed.append,
            ),
        )
        return code, "\n".join(echoed)

    yield run_it, graph, store
    store.close()


def test_report_only_run_writes_nothing(journey):
    run_it, graph, store = journey
    code, out = run_it()
    assert code == 0
    assert "report only" in out and HOST in out
    assert graph.emitted == []
    assert [e["kind"] for e in store.timeline("RUN1")] == []


def test_approved_plan_applies_to_the_agents_top_suspect(journey):
    run_it, graph, store = journey
    code, _out = run_it(remediate=["set_attrs:state=drained"], answers=["1"])
    assert code == 0
    # the step hit the suspect the agent named, not the alerting resource
    assert graph.attrs(HOST) == {"state": "drained"}
    assert graph.attrs(VM) == {"role": "web"}
    kinds = [e["kind"] for e in store.timeline("RUN1")]
    assert "approval" in kinds and "step" in kinds


def test_the_upsert_keeps_edges_the_patch_never_mentioned(journey):
    run_it, graph, _store = journey
    code, _out = run_it(suspects=(VM,), remediate=["set_attrs:role=drained"], answers=["1"])
    assert code == 0
    assert graph.rels[VM] == [("RUNS_ON", HOST)]


def test_a_rejected_plan_applies_nothing_but_is_still_recorded(journey):
    run_it, graph, store = journey
    code, out = run_it(remediate=["set_attrs:state=drained"], answers=["no"])
    assert code == 0
    assert "rejected" in out
    assert graph.emitted == []
    approval = [e for e in store.timeline("RUN1") if e["kind"] == "approval"]
    assert approval and approval[0]["payload"]["approved"] is False


def test_a_mistyped_count_re_asks_rather_than_applying(journey):
    run_it, graph, _store = journey
    code, out = run_it(remediate=["set_attrs:state=drained"], answers=["3", "1"])
    assert code == 0
    assert "count them and retype" in out
    assert graph.attrs(HOST) == {"state": "drained"}


def test_an_explicit_target_overrides_the_suspect(journey):
    run_it, graph, _store = journey
    code, _out = run_it(remediate=[f"set_attrs@{VM}:role=drained"], answers=["1"])
    assert code == 0
    assert graph.attrs(VM) == {"role": "drained"}


def test_no_suspect_means_no_plan_is_proposed(journey):
    run_it, graph, _store = journey
    code, out = run_it(suspects=(), remediate=["set_attrs:state=x"])
    assert code == 1
    assert "refusing to propose a plan against nothing" in out
    assert graph.emitted == []


def test_the_plan_the_approver_sees_declares_the_rollback_state(journey):
    run_it, _graph, _store = journey
    _code, out = run_it(remediate=["set_attrs:state=drained"], answers=["no"])
    assert "current:" in out
    assert json.dumps({"state": "drained"}, sort_keys=True) in out


def test_a_step_that_cannot_apply_exits_nonzero(journey):
    run_it, graph, store = journey
    code, _out = run_it(remediate=["set_attrs@vm-999999:role=x"], answers=["1"])
    assert code == 1
    assert graph.emitted == []
    steps = [e for e in store.timeline("RUN1") if e["kind"] == "step"]
    assert any(e["payload"]["status"] == "failed" for e in steps)


def test_live_summary_is_built_from_the_graph(journey):
    _run_it, graph, _store = journey
    summary = live_summary(FakeSession(graph), VM, datetime.fromisoformat(FIRED), 24)
    assert summary.resource_counts == {"vm": 1, "host": 1}
    assert f"{VM} runs_on {HOST}" in summary.neighborhood
    assert summary.window_end.isoformat() == FIRED


def test_remediation_without_an_approver_is_refused_before_any_io():
    """Argument guards live in the command, and reject before the
    journey — so this needs no stores and no stubs."""
    result = runner.invoke(app, ["triage", VM, "--remediate", "set_attrs:state=drained"])
    assert result.exit_code != 0
    assert "--approver" in result.output and "--remediate" in result.output


def test_a_naive_fired_at_is_refused_before_any_io():
    result = runner.invoke(app, ["triage", VM, "--fired-at", "2026-01-02T03:04:05"])
    assert result.exit_code != 0
    assert "offset" in result.output


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
