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

The command's composition root gets its own two tests at the bottom,
and those DO stub what it constructs — building those collaborators
and closing them is the whole of its job, so stubbing them tests its
contract rather than working around it.
"""

import json
import re
from datetime import datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from resgraph.analyst.audit import AuditStore
from resgraph.analyst.cli import (
    TriageIO,
    _git_ref,
    _print_report,
    app,
    live_summary,
    parse_remediation,
    triage_journey,
)
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
        self.closed = False

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
        self.closed = True


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

    def run_it(
        *, suspects=(HOST,), remediate=(), answers=(), approver="fran", dry_run=False, wired=True
    ):
        code = triage_journey(
            resource_id=VM,
            symptom="crash_loop",
            fired_at=datetime.fromisoformat(FIRED),
            remediate=list(remediate),
            approver=approver,
            dry_run=dry_run,
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
                emit=graph.emit if wired else None,
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


def _plain(text: str) -> str:
    """Typer renders usage errors in a rich box whose wrapping follows
    terminal width; compare against the words, not the layout."""
    return re.sub(r"[^\w\s.:=@-]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))


def test_a_dry_run_without_steps_is_refused_before_any_io():
    result = runner.invoke(app, ["triage", VM, "--fired-at", FIRED, "--dry-run"])
    assert result.exit_code != 0
    assert "nothing to preview" in _plain(result.output)


def test_remediation_without_an_approver_is_refused_before_any_io():
    """Argument guards live in the command, and reject before the
    journey — so this needs no stores and no stubs."""
    result = runner.invoke(
        app, ["triage", VM, "--fired-at", FIRED, "--remediate", "set_attrs:state=drained"]
    )
    assert result.exit_code != 0
    plain = _plain(result.output)
    assert "--approver" in plain and "--remediate" in plain


def test_a_naive_fired_at_is_refused_before_any_io():
    result = runner.invoke(app, ["triage", VM, "--fired-at", "2026-01-02T03:04:05"])
    assert result.exit_code != 0
    assert "offset" in _plain(result.output)


def test_an_omitted_fired_at_is_refused_not_defaulted_to_wall_clock():
    """A wall-clock default in a seeded world would place the alert
    months ahead of every event in the store."""
    result = runner.invoke(app, ["triage", VM])
    assert result.exit_code != 0
    assert "--fired-at" in _plain(result.output)


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


def test_parse_remediation_rejects_a_missing_action():
    with pytest.raises(Exception, match="no action"):
        parse_remediation(f"@{VM}:role=x", "top")


def test_an_unparseable_report_is_said_plainly(journey):
    run_it, graph, _store = journey
    printed: list[str] = []
    _print_report(None, printed.append)
    assert "no parseable report" in printed[0]
    assert graph.emitted == []


def test_remediation_without_a_write_channel_refuses_rather_than_pretending(tmp_path):
    """The journey is handed no emit when the command opened no sink;
    it must not reach the executor and quietly do nothing."""
    graph = Graph()
    store = AuditStore(tmp_path / "audit.db")
    try:
        with pytest.raises(RuntimeError, match="without a write channel"):
            triage_journey(
                resource_id=VM,
                symptom="crash_loop",
                fired_at=datetime.fromisoformat(FIRED),
                remediate=["set_attrs:state=drained"],
                approver="fran",
                model="claude-test",
                window_h=24,
                run_id="RUN1",
                git_ref="abc1234",
                io=TriageIO(
                    session=FakeSession(graph),
                    client=object(),
                    toolset=object(),
                    store=store,
                    run=_agent(_report([HOST])),
                    emit=None,
                    ask=Script(),
                    echo=lambda _m: None,
                ),
            )
    finally:
        store.close()
    assert graph.emitted == []


def test_git_ref_reads_the_repo_and_degrades_to_unknown_outside_one(tmp_path, monkeypatch):
    assert re.fullmatch(r"[0-9a-f]{7,}", _git_ref())
    monkeypatch.chdir(tmp_path)
    assert _git_ref() == "unknown"


# --- the composition root: what the command builds, and what it closes ---


class WiringDriver:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def verify_connectivity(self) -> None:
        pass

    def session(self) -> FakeSession:
        return self._session


class WiringSink:
    def __init__(self) -> None:
        self.closed = False
        self.emitted: list[Any] = []

    def emit_many(self, msgs: list[Any]) -> None:
        self.emitted.extend(msgs)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def wiring(monkeypatch, tmp_path):
    session, sink, captured = FakeSession(Graph()), WiringSink(), {}

    def fake_journey(**kw):
        captured.update(kw)
        return captured.get("code", 0)

    monkeypatch.setattr("resgraph.analyst.cli.triage_journey", fake_journey)
    monkeypatch.setattr("resgraph.graph.client.get_driver", lambda: WiringDriver(session))
    monkeypatch.setattr("anthropic.Anthropic", object)
    monkeypatch.setattr("resgraph.analyst.tools.default_toolset", object)
    monkeypatch.setattr("resgraph.gen.sinks.RedisSink", lambda *a, **k: sink)
    return session, sink, captured, str(tmp_path / "audit.db")


def test_the_command_hands_the_journey_what_it_built(wiring):
    session, sink, captured, db = wiring
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--fired-at",
            FIRED,
            "--db",
            db,
            "--approver",
            "fran",
            "--remediate",
            "set_attrs:state=x",
        ],
    )
    assert result.exit_code == 0, result.output
    io = captured["io"]
    assert io.session is session
    assert io.emit == sink.emit_many
    assert captured["remediate"] == ["set_attrs:state=x"]
    assert session.closed and sink.closed


def test_a_report_only_run_opens_no_stream_at_all(wiring):
    session, sink, captured, db = wiring
    result = runner.invoke(app, ["triage", VM, "--fired-at", FIRED, "--db", db])
    assert result.exit_code == 0, result.output
    assert captured["io"].emit is None
    assert not sink.closed  # never constructed, so nothing to close
    assert session.closed


def test_the_journeys_exit_code_reaches_the_shell_and_nothing_leaks(wiring, monkeypatch):
    session, sink, _captured, db = wiring

    def failing_journey(**kw):
        raise RuntimeError("the journey blew up")

    monkeypatch.setattr("resgraph.analyst.cli.triage_journey", failing_journey)
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--fired-at",
            FIRED,
            "--db",
            db,
            "--approver",
            "fran",
            "--remediate",
            "set_attrs:state=x",
        ],
    )
    assert result.exit_code != 0
    assert session.closed and sink.closed, "an exception must still close the session and the sink"


def test_a_nonzero_journey_becomes_a_nonzero_exit(wiring):
    session, sink, captured, db = wiring
    captured["code"] = 1
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--fired-at",
            FIRED,
            "--db",
            db,
            "--approver",
            "fran",
            "--remediate",
            "set_attrs:state=x",
        ],
    )
    assert result.exit_code == 1
    assert session.closed and sink.closed


def test_dry_run_previews_without_approval_or_a_stream(journey):
    """emit=None is what the command passes on --dry-run; the guard
    must not fire first (#159 review: the original ordering crashed)."""
    run_it, graph, store = journey
    code, out = run_it(remediate=["set_attrs:state=drained"], dry_run=True, wired=False)
    assert code == 0
    assert "dry run" in out
    assert '"op":"upsert"' in out and '"state":"drained"' in out
    assert graph.emitted == []
    assert [e["kind"] for e in store.timeline("RUN1")] == [], "nothing to approve, nothing recorded"


def test_the_grants_expiry_is_on_the_audit_trail(journey):
    """The lifetime is part of the decision, so it is on the trail."""
    run_it, _graph, store = journey
    run_it(remediate=["set_attrs:state=drained"], answers=["1"])
    approval = [e for e in store.timeline("RUN1") if e["kind"] == "approval"]
    assert approval, "the approval should be on the trail"
    expires = approval[0]["payload"]["expires_at"]
    parsed = datetime.fromisoformat(expires)
    assert parsed.tzinfo is not None, "an aware deadline, per D2's posture"
    assert parsed > datetime.fromisoformat(approval[0]["ts"].replace("Z", "+00:00"))


def test_dry_run_wiring_builds_no_sink_and_passes_no_emit(wiring):
    """The flag changes what the composition root constructs (#159)."""
    session, sink, captured, db = wiring
    result = runner.invoke(
        app,
        [
            "triage",
            VM,
            "--fired-at",
            FIRED,
            "--db",
            db,
            "--dry-run",
            "--remediate",
            "set_attrs:state=x",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["dry_run"] is True
    assert captured["io"].emit is None
    assert not sink.closed, "no sink is ever constructed for a preview"
    assert session.closed
