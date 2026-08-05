"""Harness invariants — budgets degrade, retries append, replay is verbatim."""

import json
from types import SimpleNamespace

import pytest

from resgraph.analyst.harness import Prompt, parse_and_validate, run_triage
from resgraph.analyst.tools import RegistryToolset, ToolOutcome
from resgraph.tools.registry import TOOL_REGISTRY

MODEL = "claude-test"

PROMPT = Prompt(
    system=[
        {"type": "text", "text": "You are the analyst.", "cache_control": {"type": "ephemeral"}}
    ],
    user=(
        "Alert: unreachable on vm-000002, fired at 2026-01-01T00:00:01Z. "
        "World summary: vm-000002 runs on host-000001."
    ),
)

ALL_TRUE = {"mechanism_verified": True, "event_found": True, "explains_symptom": True}
HEDGED = {"mechanism_verified": True, "event_found": False, "explains_symptom": False}

VALID_REPORT = json.dumps(
    {
        "suspects": [
            {
                "sequence": 41,
                "resource_id": "host-000001",
                "mechanism_path": ["host-000001", "vm-000002"],
                "verdict": ALL_TRUE,
                "confidence": "high",
                "evidence": ["host-000001 state flipped to down at seq 41"],
            }
        ],
        "no_confident_candidate": False,
        "narrative": "The host under the VM went down.",
    }
)

HEDGED_REPORT = json.dumps(
    {
        "suspects": [
            {
                "sequence": 41,
                "resource_id": "host-000001",
                "mechanism_path": ["host-000001", "vm-000002"],
                "verdict": HEDGED,
                "confidence": "low",
                "evidence": ["correlate only; event not re-read"],
            }
        ],
        "no_confident_candidate": True,
        "narrative": "Hedged.",
    }
)


def usage(inp=100, out=50, cache_read=0, cache_creation=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def text(t):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="considering the host", signature="sig")


def tool_use(uid, name, args):
    return SimpleNamespace(type="tool_use", id=uid, name=name, input=args)


def response(*blocks, u=None):
    return SimpleNamespace(content=list(blocks), usage=u or usage())


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


class FakeToolset:
    def __init__(self, payload=None):
        self.calls = []
        self._payload = payload or json.dumps(
            {"found": True, "id": "host-000001", "sequence": 41, "relationships": []}
        )

    def blocks(self):
        return [{"name": "fetch_resource", "description": "d", "input_schema": {"type": "object"}}]

    def execute(self, name, args):
        self.calls.append((name, args))
        return ToolOutcome(ok=True, payload=self._payload)


def test_tool_loop_replays_assistant_content_verbatim():
    first = response(thinking(), tool_use("t1", "fetch_resource", {"resource_id": "host-000001"}))
    client = FakeClient([first, response(text(VALID_REPORT))])
    toolset = FakeToolset()
    result = run_triage(PROMPT, toolset, client, model=MODEL)

    assert result.report is not None
    assert result.tool_calls == 1
    assert toolset.calls == [("fetch_resource", {"resource_id": "host-000001"})]
    second_request = client.requests[1]["messages"]
    assert second_request[1]["role"] == "assistant"
    assert second_request[1]["content"] is first.content
    tool_results = second_request[2]["content"]
    assert tool_results[0]["type"] == "tool_result"
    assert tool_results[0]["tool_use_id"] == "t1"
    assert not tool_results[0]["is_error"]


def test_budget_exhaustion_refuses_and_degrades():
    calls = [
        response(tool_use(f"t{i}", "fetch_resource", {"resource_id": "host-000001"}))
        for i in range(3)
    ]
    client = FakeClient([*calls, response(text(VALID_REPORT))])
    toolset = FakeToolset()
    result = run_triage(PROMPT, toolset, client, model=MODEL, max_tool_calls=2)

    assert result.degraded
    assert result.cutoff_reason == "tool_calls"
    assert result.tool_calls == 2
    assert len(toolset.calls) == 2
    refusal = client.requests[3]["messages"][-1]["content"][0]
    assert refusal["is_error"]
    assert "budget exhausted" in refusal["content"].lower()


def test_cost_ceiling_injects_conclude_now_and_records_reason():
    # Turn 1 spends over the ceiling; the next turn boundary trips the
    # cutoff, injects the conclude-now message, and the model concludes.
    client = FakeClient(
        [
            response(tool_use("t1", "fetch_resource", {}), u=usage(inp=500, out=50)),
            response(text(HEDGED_REPORT)),
        ]
    )
    # After turn 1 the meter reads over budget, so turn 2's boundary
    # check trips the cutoff before another tool call is offered.
    result = run_triage(
        PROMPT,
        FakeToolset(),
        client,
        model=MODEL,
        max_cost_usd=1.0,
        cost_fn=lambda u: 2.0 if u.total_input else 0.0,
    )
    assert result.degraded
    assert result.cutoff_reason == "cost"
    assert result.report is not None  # an exception is not a conclusion; a report is
    injected = client.requests[-1]["messages"][-1]
    assert injected["role"] == "user"
    assert "conclude now" in injected["content"][0]["text"].lower()


def test_wall_clock_ceiling_trips_at_zero():
    client = FakeClient([response(text(HEDGED_REPORT))])
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL, max_wall_s=0.0)
    assert result.degraded and result.cutoff_reason == "wall_clock"
    assert result.report is not None


def test_cost_ceiling_requires_its_meter():
    client = FakeClient([response(text(VALID_REPORT))])
    with pytest.raises(ValueError, match="needs its meter"):
        run_triage(PROMPT, FakeToolset(), client, model=MODEL, max_cost_usd=1.0)


def test_no_ceilings_leaves_cutoff_reason_none():
    client = FakeClient([response(text(HEDGED_REPORT))])
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    assert result.cutoff_reason is None and not result.degraded


def test_token_ceiling_refuses_tools():
    client = FakeClient(
        [
            response(tool_use("t1", "fetch_resource", {}), u=usage(inp=50_000, out=10_000)),
            response(text(HEDGED_REPORT)),
        ]
    )
    toolset = FakeToolset()
    result = run_triage(PROMPT, toolset, client, model=MODEL, max_run_tokens=1_000)
    assert result.degraded
    assert toolset.calls == []


def test_validation_retry_appends_new_message_system_untouched():
    client = FakeClient([response(text("not json at all")), response(text(HEDGED_REPORT))])
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)

    assert result.report is not None
    assert result.validation_failures == ["no JSON object found in the reply"]
    a, b = client.requests
    assert json.dumps(a["system"]) == json.dumps(b["system"])
    assert b["messages"][-1]["role"] == "user"
    assert "failed validation" in b["messages"][-1]["content"][0]["text"]


def test_referential_check_rejects_unseen_resource():
    bad = json.loads(HEDGED_REPORT)
    bad["suspects"][0]["resource_id"] = "host-999999"
    bad["suspects"][0]["mechanism_path"] = ["host-999999", "vm-000002"]
    client = FakeClient([response(text(json.dumps(bad))), response(text(HEDGED_REPORT))])
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)

    assert result.report is not None
    assert any("host-999999" in f for f in result.validation_failures)


def test_retries_exhausted_returns_no_report():
    client = FakeClient([response(text("nope"))] * 3)
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    assert result.report is None
    assert len(client.requests) == 3


def test_report_degraded_flag_propagates():
    flagged = json.loads(HEDGED_REPORT)
    flagged["degraded"] = True
    client = FakeClient([response(text(json.dumps(flagged)))])
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    assert result.degraded


def test_usage_accumulates_and_cache_rate():
    client = FakeClient(
        [
            response(
                tool_use("t1", "fetch_resource", {}),
                u=usage(inp=100, out=10, cache_read=900, cache_creation=0),
            ),
            response(text(VALID_REPORT), u=usage(inp=100, out=10, cache_read=900)),
        ]
    )
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    assert result.usage.total_input == 2_000
    assert result.usage.cache_hit_rate == pytest.approx(0.9)


def test_parse_and_validate_reads_fenced_json():
    report, errors = parse_and_validate(
        f"Here is the report:\n```json\n{VALID_REPORT}\n```",
        {"host-000001", "vm-000002"},
        {41},
    )
    assert errors == []
    assert report is not None and report.suspects[0].resource_id == "host-000001"


def test_registry_toolset_derives_all_read_tools():
    toolset = RegistryToolset(lambda: (_ for _ in ()).throw(AssertionError("no store touch")))
    blocks = toolset.blocks()
    assert [b["name"] for b in blocks] == [e.name for e in TOOL_REGISTRY]
    assert all(b["input_schema"].get("properties") is not None for b in blocks)


def test_registry_toolset_refuses_non_read_only():
    entry = TOOL_REGISTRY[0]
    mutated = type(entry.hints)(
        read_only=False, destructive=True, idempotent=False, open_world=False
    )
    bad = type(entry)(
        name=entry.name,
        fn=entry.fn,
        input_model=entry.input_model,
        output_model=entry.output_model,
        description=entry.description,
        surfaces=entry.surfaces,
        scopes=entry.scopes,
        privileged=entry.privileged,
        hints=mutated,
        timeout_s=entry.timeout_s,
    )
    with pytest.raises(RuntimeError, match="refuses"):
        RegistryToolset(lambda: None, entries=(bad,))


def test_registry_toolset_invalid_input_is_steering_error():
    toolset = RegistryToolset(lambda: (_ for _ in ()).throw(AssertionError("no store touch")))
    outcome = toolset.execute("fetch_resource", {"nope": 1})
    assert not outcome.ok
    payload = json.loads(outcome.payload)
    assert payload["error_class"] == "invalid_input"
    assert payload["action"] == "rephrase"


def test_registry_toolset_unknown_tool():
    toolset = RegistryToolset(lambda: (_ for _ in ()).throw(AssertionError("no store touch")))
    outcome = toolset.execute("drop_tables", {})
    assert not outcome.ok
    assert json.loads(outcome.payload)["error_class"] == "invalid_input"


def test_flag_must_equal_verdict_arithmetic():
    inconsistent = json.loads(VALID_REPORT)
    inconsistent["no_confident_candidate"] = True  # while an all-true verdict exists
    client = FakeClient(
        [
            response(tool_use("t1", "fetch_resource", {"resource_id": "host-000001"})),
            response(text(json.dumps(inconsistent))),
            response(text(VALID_REPORT)),
        ]
    )
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    assert result.report is not None
    assert not result.report.no_confident_candidate
    assert any("must be exactly" in f for f in result.validation_failures)


def test_event_found_requires_a_seen_nonzero_sequence():
    _, errors = parse_and_validate(VALID_REPORT, {"host-000001", "vm-000002"}, set())
    assert any("never appeared in this run's tool results" in e for e in errors)
    snapshot_claim = json.loads(VALID_REPORT)
    snapshot_claim["suspects"][0]["sequence"] = 0
    _, errors = parse_and_validate(
        json.dumps(snapshot_claim), {"host-000001", "vm-000002"}, {0, 41}
    )
    assert any("initial snapshot, not a change event" in e for e in errors)
