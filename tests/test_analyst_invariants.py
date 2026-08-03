"""The invariants the cache and the graders stand on: prefix bytes
frozen across a whole run, the transcript only grows, thinking blocks
replay verbatim, and the generator and grader agree on the graph."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from test_analyst_harness import FakeToolset, response, text, thinking, tool_use

from resgraph.analyst.harness import run_triage
from resgraph.analyst.prompts import WorldSummary, build_prompt
from resgraph.cold import store as cold_store
from resgraph.evals.runner import evidence_inputs
from resgraph.gen.scenarios import (
    CAUSAL_TYPES,
    ScenarioType,
    edges_at,
    generate_scenario,
)

MODEL = "claude-test"

PROMPT = build_prompt(
    resource_id="vm-000002",
    symptom="unreachable",
    fired_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
    summary=WorldSummary(
        resource_counts={"vm": 18, "host": 3},
        neighborhood=("vm-000002 runs_on host-000001",),
        window_start=datetime(2026, 1, 1, tzinfo=UTC),
        window_end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
    ),
)

VALID_REPORT = json.dumps(
    {
        "suspects": [
            {
                "sequence": 41,
                "resource_id": "host-000001",
                "mechanism_path": ["host-000001", "vm-000002"],
                "confidence": "high",
                "evidence": ["host-000001 went down"],
            }
        ],
        "no_confident_candidate": False,
        "narrative": "The host under the VM went down.",
    }
)


class SnapshottingClient:
    """Copies request payloads at call time — run_triage mutates its
    messages list in place, so a late inspection of a shared reference
    would only ever see the final transcript."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        marks = [
            (i, j)
            for i, m in enumerate(kwargs["messages"])
            if isinstance(m["content"], list)
            for j, block in enumerate(m["content"])
            if isinstance(block, dict) and "cache_control" in block
        ]
        self.requests.append(
            {
                "system_bytes": json.dumps(kwargs["system"], sort_keys=True, default=str),
                "tools_bytes": json.dumps(kwargs["tools"], sort_keys=True, default=str),
                "messages": list(kwargs["messages"]),
                "marks_at_call_time": marks,
            }
        )
        return self._responses.pop(0)


def full_run_client():
    return SnapshottingClient(
        [
            response(thinking(), tool_use("t1", "fetch_resource", {"resource_id": "host-000001"})),
            response(text("not a report")),
            response(text(VALID_REPORT)),
        ]
    )


def test_prefix_bytes_identical_across_every_request():
    client = full_run_client()
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    assert result.report is not None
    assert len(client.requests) == 3
    system_variants = {r["system_bytes"] for r in client.requests}
    tools_variants = {r["tools_bytes"] for r in client.requests}
    assert len(system_variants) == 1, "system bytes changed mid-run — cache busted"
    assert len(tools_variants) == 1, "tool bytes changed mid-run — implicit prefix busted"


def test_transcript_only_grows():
    client = full_run_client()
    run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    for prev, nxt in zip(client.requests, client.requests[1:], strict=False):
        prev_msgs, nxt_msgs = prev["messages"], nxt["messages"]
        assert len(nxt_msgs) > len(prev_msgs)
        for a, b in zip(prev_msgs, nxt_msgs, strict=False):
            assert a is b, "an earlier message was replaced, not appended to"


def test_message_order_and_thinking_replay():
    client = full_run_client()
    run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    final = client.requests[-1]["messages"]
    assert [m["role"] for m in final] == ["user", "assistant", "user", "assistant", "user"]
    replayed_tool_turn = final[1]["content"]
    assert replayed_tool_turn[0].type == "thinking"
    assert replayed_tool_turn[0].thinking == "considering the host"
    assert "failed validation" in final[4]["content"][0]["text"]


def test_transcript_breakpoint_moves_and_stays_single():
    client = full_run_client()
    run_triage(PROMPT, FakeToolset(), client, model=MODEL)
    for request in client.requests:
        last_msg = len(request["messages"]) - 1
        last_block = len(request["messages"][last_msg]["content"]) - 1
        assert request["marks_at_call_time"] == [(last_msg, last_block)], (
            "exactly one transcript breakpoint per request, always on the final block"
        )


def test_turn_cap_terminates_a_model_that_never_concludes():
    endless = [
        response(tool_use(f"t{i}", "fetch_resource", {"resource_id": "host-000001"}))
        for i in range(30)
    ]
    client = SnapshottingClient(endless)
    result = run_triage(PROMPT, FakeToolset(), client, model=MODEL, max_tool_calls=2)
    assert result.report is None
    assert result.degraded
    assert result.turns == 2 + 2 + 5  # max_tool_calls + retries + slack


def test_generator_and_grader_agree_on_the_graph(tmp_path):
    """The D25 precondition, tested once, deliberately: the generator's
    log-replay view of the graph must equal the cold store's as-of
    answer that the evidence grader uses — for every scenario type, at
    planting time and at alert time."""
    for i, kind in enumerate([*CAUSAL_TYPES, ScenarioType.CONTROL]):
        gen = generate_scenario(kind, seed=13)
        catalog = cold_store.get_catalog(tmp_path / f"cold-{i}")
        cold_store.ensure_tables(catalog)
        cold_store.append_events(catalog, gen.messages)
        moments = [gen.spec.alert.fired_at]
        if gen.spec.ground_truth is not None:
            cause = next(
                m for m in gen.messages if m.sequence == gen.spec.ground_truth.causal_sequence
            )
            moments.append(cause.event_time)
        for t in moments:
            store_edges, store_sequences = evidence_inputs(catalog, t)
            assert store_edges == edges_at(gen.messages, t), (kind, t)
        assert store_sequences == {m.sequence for m in gen.messages}, kind
        if gen.spec.ground_truth is not None:
            path = gen.spec.ground_truth.mechanism_path
            t0_edges, _ = evidence_inputs(catalog, moments[-1])
            for dependency, dependent in zip(path, path[1:], strict=False):
                assert (dependent, dependency) in t0_edges, (kind, path)
