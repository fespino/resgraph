"""The audit CLI (D27): every surface exercised against a real store
file — timeline, --touched, --trace, --tool/--since — plus the
argument contract."""

import pytest
from test_approval import gate, plan
from typer.testing import CliRunner

from resgraph.analyst.audit import AuditStore
from resgraph.analyst.cli import app
from resgraph.analyst.remediation import StepMachine

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "audit.db"
    store = AuditStore(path)
    store.begin_run("r1", alert={"symptom": "crash_loop"}, model="claude-test", git_ref="abc1234")
    sink = store.sink("r1")
    sink("llm_call", {"turn": 1, "tool_uses": 2, "latency_ms": 1200, "tokens": 3500})
    sink("tool_call", {"tool": "world_diff", "args": {}, "ok": True, "ids": ["sg-000108"]})
    sink("tool_call", {"tool": "resource_history", "args": {}, "ok": False, "ids": []})
    sink("llm_call", {"turn": 2, "tool_uses": 0, "latency_ms": 900, "tokens": 2100})
    sink("cutoff", {"reason": "tool_calls", "calls_used": 15, "tokens_spent": 90000})
    p = plan(2)
    decision, _ = gate(p, "skip 2", "1")
    store.record_approval("r1", decision)
    approved = [p[i] for i in decision.applied]
    m = StepMachine(
        approved, run_id="r1", owner="fran", apply=lambda s: "ref", rollback=lambda s: None
    )
    m.execute()
    store.record_step_events("r1", m.events, approved)
    store.close()
    return str(path)


def test_timeline_renders_every_kind(db):
    result = runner.invoke(app, ["audit", "r1", "--db", db])
    assert result.exit_code == 0
    out = result.output
    assert "run r1  model=claude-test" in out
    assert "turn 1 → 2 tool use(s)" in out and "1200ms" in out and "3500tok" in out
    assert "world_diff ok" in out and "resource_history ERROR" in out
    assert "tool_calls exhausted at call 15" in out
    assert "approved by fran" in out and "1 applied, 1 skipped" in out
    assert "a0 #0 succeeded t0" in out


def test_touched_lists_read_and_written(db):
    result = runner.invoke(app, ["audit", "r1", "--touched", "--db", db])
    assert result.exit_code == 0
    assert "read:    sg-000108" in result.output
    assert "written: t0" in result.output


def test_trace_nests_tool_calls_under_their_llm_call(db):
    result = runner.invoke(app, ["audit", "r1", "--trace", "--db", db])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    diff = next(line for line in lines if "world_diff" in line)
    history = next(line for line in lines if "resource_history" in line)
    assert diff.startswith("│  ├─") and history.startswith("│  └─")
    assert lines[-1].startswith("└─")


def test_tool_history_crosses_runs(db):
    result = runner.invoke(
        app, ["audit", "--tool", "apply_remediation", "--since", "7d", "--db", db]
    )
    assert result.exit_code == 0
    assert result.output.count("r1") == 2  # started + succeeded


def test_run_id_or_tool_required(db):
    result = runner.invoke(app, ["audit", "--db", db])
    assert result.exit_code != 0
    assert "run_id" in result.output


def test_bad_since_rejected(db):
    result = runner.invoke(
        app, ["audit", "--tool", "world_diff", "--since", "fortnight", "--db", db]
    )
    assert result.exit_code != 0


def test_verify_reports_intact_chain(db):
    result = runner.invoke(app, ["audit", "r1", "--verify", "--db", db])
    assert result.exit_code == 0
    assert "chain ok" in result.output


def test_verify_fails_loud_on_tampering(db):
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE events SET payload = '{\"forged\": true}' WHERE seq=2")
    conn.commit()
    conn.close()
    result = runner.invoke(app, ["audit", "r1", "--verify", "--db", db])
    assert result.exit_code == 1
    assert "chain broken at seq 2" in result.output
