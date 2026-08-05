"""D27 audit-trail invariants: every query answered from the store
alone, with the agent stopped."""

from datetime import UTC, datetime, timedelta

import pytest
from test_analyst_harness import (
    PROMPT,
    VALID_REPORT,
    FakeClient,
    FakeToolset,
    response,
    text,
    thinking,
    tool_use,
)
from test_approval import gate, plan

from resgraph.analyst.audit import AuditStore, parse_since
from resgraph.analyst.harness import run_triage
from resgraph.analyst.remediation import StepMachine

MODEL = "claude-test"


@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path / "audit.db")
    yield s
    s.close()


def triage_with_sink(store, run_id="r1", max_tool_calls=15):
    client = FakeClient(
        [
            response(tool_use("t1", "fetch_resource", {"resource_id": "host-000001"})),
            response(text(VALID_REPORT)),
        ]
    )
    result = run_triage(
        PROMPT,
        FakeToolset(),
        client,
        model=MODEL,
        max_tool_calls=max_tool_calls,
        on_event=store.sink(run_id),
    )
    return result


def test_sink_orders_events_and_lifts_columns(store):
    triage_with_sink(store)
    events = store.timeline("r1")
    assert [e["seq"] for e in events] == list(range(len(events)))
    kinds = [e["kind"] for e in events]
    assert kinds == ["llm_call", "tool_call", "llm_call"]
    llm = events[0]
    assert llm["tokens"] == 150 and llm["latency_ms"] is not None
    assert "tokens" not in llm["payload"] and "latency_ms" not in llm["payload"]
    call = events[1]
    assert call["payload"]["tool"] == "fetch_resource"
    assert call["payload"]["ids"] == ["host-000001"]


def test_cutoff_event_emitted_once_with_reason(store):
    client = FakeClient(
        [
            response(
                tool_use("t1", "fetch_resource", {"resource_id": "host-000001"}),
                tool_use("t2", "fetch_resource", {"resource_id": "host-000001"}),
            ),
            response(text(VALID_REPORT)),
        ]
    )
    run_triage(
        PROMPT,
        FakeToolset(),
        client,
        model=MODEL,
        max_tool_calls=1,
        on_event=store.sink("r1"),
    )
    cutoffs = [e for e in store.timeline("r1") if e["kind"] == "cutoff"]
    assert len(cutoffs) == 1
    assert cutoffs[0]["payload"]["reason"] == "tool_calls"


def test_run_row_lifecycle(store):
    store.begin_run("r1", alert={"symptom": "unreachable"}, model=MODEL, git_ref="abc123")
    result = triage_with_sink(store)
    store.finish_run("r1", result)
    row = store.run_row("r1")
    assert row["model"] == MODEL and row["git_ref"] == "abc123"
    assert row["tool_calls"] == 1 and not row["degraded"]
    assert row["finished_at"] >= row["started_at"]
    assert '"suspects": 1' in row["verdict"]


def test_touched_answers_read_and_written(store):
    triage_with_sink(store)
    p = plan(2)
    machine = StepMachine(
        p, run_id="r1", owner="fran", apply=lambda s: "ref", rollback=lambda s: None
    )
    machine.execute()
    store.record_step_events("r1", machine.events, p)
    touched = store.touched("r1")
    assert touched["read"] == ["host-000001"]
    assert touched["written"] == ["t0", "t1"]


def test_tool_history_filters_by_window(store):
    p = plan(1)
    old = StepMachine(p, run_id="old", owner="f", apply=lambda s: "r", rollback=lambda s: None)
    old.execute()
    stale = [
        e.model_copy(update={"timestamp": datetime.now(UTC) - timedelta(days=8)})
        for e in old.events
    ]
    store.record_step_events("old", stale, p)
    fresh = StepMachine(p, run_id="new", owner="f", apply=lambda s: "r", rollback=lambda s: None)
    fresh.execute()
    store.record_step_events("new", fresh.events, p)
    week = store.tool_history("apply_remediation", since=timedelta(days=7))
    assert {r["run_id"] for r in week} == {"new"}
    assert len(store.tool_history("apply_remediation")) == 4


def test_approval_recorded_with_time_to_decision(store):
    decision, _ = gate(plan(), "3")
    store.record_approval("r1", decision)
    (event,) = store.timeline("r1")
    assert event["kind"] == "approval"
    assert event["payload"]["approved"] and event["payload"]["approver"] == "fran"
    assert event["latency_ms"] == decision.time_to_decision_ms


def test_llm_call_records_reasoning_when_returned(store):
    client = FakeClient(
        [
            response(thinking(), tool_use("t1", "fetch_resource", {"resource_id": "host-000001"})),
            response(text(VALID_REPORT)),
        ]
    )
    run_triage(PROMPT, FakeToolset(), client, model=MODEL, on_event=store.sink("r1"))
    llm = [e for e in store.timeline("r1") if e["kind"] == "llm_call"]
    assert llm[0]["payload"]["thinking_form"] == "recorded"
    assert "considering the host" in llm[0]["payload"]["thinking"]
    assert llm[1]["payload"]["thinking_form"] == "absent"
    assert "thinking" not in llm[1]["payload"]


def test_llm_call_labels_elided_reasoning(store):
    from types import SimpleNamespace

    empty = SimpleNamespace(type="thinking", thinking="", signature="sig")
    client = FakeClient(
        [
            response(empty, tool_use("t1", "fetch_resource", {"resource_id": "host-000001"})),
            response(text(VALID_REPORT)),
        ]
    )
    run_triage(PROMPT, FakeToolset(), client, model=MODEL, on_event=store.sink("r1"))
    llm = [e for e in store.timeline("r1") if e["kind"] == "llm_call"]
    assert llm[0]["payload"]["thinking_form"] == "elided"
    assert "thinking" not in llm[0]["payload"]


def test_chain_verifies_across_all_writers(store):
    store.begin_run("r1", alert={"symptom": "unreachable"}, model=MODEL, git_ref="abc")
    triage_with_sink(store)
    decision, _ = gate(plan(), "3")
    store.record_approval("r1", decision)
    p = plan(1)
    m = StepMachine(p, run_id="r1", owner="fran", apply=lambda s: "r", rollback=lambda s: None)
    m.execute()
    store.record_step_events("r1", m.events, p)
    assert store.verify_chain("r1") is None


def test_chain_names_the_tampered_row(store):
    triage_with_sink(store)
    store._conn.execute(
        "UPDATE events SET payload = json_set(payload, '$.tool', 'world_diff')"
        " WHERE run_id='r1' AND seq=1"
    )
    store._conn.commit()
    assert store.verify_chain("r1") == 1


def test_chain_breaks_on_mid_trail_deletion(store):
    triage_with_sink(store)
    store._conn.execute("DELETE FROM events WHERE run_id='r1' AND seq=1")
    store._conn.commit()
    assert store.verify_chain("r1") == 2  # seq 2 chained through the missing row


def test_parse_since():
    assert parse_since("7d") == timedelta(days=7)
    assert parse_since("24h") == timedelta(hours=24)
    assert parse_since("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError, match="--since"):
        parse_since("7 days")
