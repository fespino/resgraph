"""The relay offline: SSE emission, TTFT/throughput from an injected clock,
the zero-token restart, the after-tokens stream_error, and slot release on
every exit including a client disconnect."""

import json

import pytest

from resgraph.gateway.dispatch import Backend
from resgraph.gateway.relay import parse_chat_sse, relay


def make_clock(step: float = 1.0):
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += step
        return state["t"]

    return clock


def admitted(name: str) -> Backend:
    b = Backend(name, concurrency=1, queue_max=4)
    b.admit()
    return b


def dying(events, reason="boom"):
    yield from events
    raise RuntimeError(reason)


def payloads(lines):
    return [json.loads(line[len("data:") :]) for line in lines if line.startswith("data:")]


def no_reopen(chain):
    pytest.fail("reopen must not be called for a death after tokens")


def test_happy_stream_relays_and_accounts():
    backend = admitted("ollama")
    events = iter(
        [("content", "a"), ("content", "b"), ("usage", {"input_tokens": 3, "output_tokens": 2})]
    )
    out = payloads(
        list(
            relay(
                alias="qwen-local-1.5b",
                backend=backend,
                events=events,
                source="override",
                fallback_chain=[],
                reopen=no_reopen,
                clock=make_clock(),
            )
        )
    )
    assert [p["type"] for p in out] == ["content", "content", "end"]
    end = out[-1]
    assert end["model"] == "qwen-local-1.5b"
    assert end["source"] == "override"
    assert end["backend"] == "ollama"
    assert end["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert end["ttft_s"] == 1.0
    assert end["tokens_per_s"] == pytest.approx(1.0)
    assert end["reconciliation_ok"] is True
    assert backend.in_flight == 0
    assert backend.ttft_ewma.value == 1.0


def test_death_after_tokens_surfaces_and_never_restarts():
    backend = admitted("ollama")
    out = payloads(
        list(
            relay(
                alias="qwen-local-1.5b",
                backend=backend,
                events=dying([("content", "a")]),
                source="override",
                fallback_chain=[],
                reopen=no_reopen,
                clock=make_clock(),
            )
        )
    )
    assert [p["type"] for p in out] == ["content", "stream_error"]
    err = out[-1]
    assert err["tokens_emitted"] == 1
    assert err["backend"] == "ollama"
    assert err["reason"] == "boom"
    assert backend.in_flight == 0


def test_zero_token_death_restarts_silently_on_the_reopened_stream():
    first = admitted("ollama")
    second = admitted("anthropic")
    chain: list[str] = []

    def reopen(chain2):
        assert chain2 == ["ollama:qwen-local-1.5b"]
        return (
            "haiku",
            second,
            iter([("content", "x"), ("usage", {"input_tokens": 1, "output_tokens": 1})]),
        )

    out = payloads(
        list(
            relay(
                alias="qwen-local-1.5b",
                backend=first,
                events=dying([]),
                source="task_class_default",
                fallback_chain=chain,
                reopen=reopen,
                clock=make_clock(),
            )
        )
    )
    assert [p["type"] for p in out] == ["content", "end"]
    end = out[-1]
    assert end["model"] == "haiku"
    assert end["backend"] == "anthropic"
    assert end["fallback_chain"] == ["ollama:qwen-local-1.5b"]
    assert first.in_flight == 0
    assert second.in_flight == 0


def test_zero_token_death_with_an_exhausted_walk_surfaces():
    backend = admitted("ollama")
    out = payloads(
        list(
            relay(
                alias="qwen-local-1.5b",
                backend=backend,
                events=dying([]),
                source="override",
                fallback_chain=[],
                reopen=lambda chain: None,
                clock=make_clock(),
            )
        )
    )
    assert [p["type"] for p in out] == ["stream_error"]
    assert out[0]["tokens_emitted"] == 0
    assert out[0]["fallback_chain"] == ["ollama:qwen-local-1.5b"]
    assert backend.in_flight == 0


def test_reconciliation_mismatch_is_reported_honestly():
    backend = admitted("ollama")
    events = iter([("content", "a"), ("usage", {"input_tokens": 1, "output_tokens": 100})])
    out = payloads(
        list(
            relay(
                alias="qwen-local-1.5b",
                backend=backend,
                events=events,
                source="override",
                fallback_chain=[],
                reopen=no_reopen,
                clock=make_clock(),
            )
        )
    )
    assert out[-1]["reconciliation_ok"] is False


def test_a_client_disconnect_releases_the_slot():
    backend = admitted("ollama")
    gen = relay(
        alias="qwen-local-1.5b",
        backend=backend,
        events=iter([("content", "a"), ("content", "b")]),
        source="override",
        fallback_chain=[],
        reopen=no_reopen,
        clock=make_clock(),
    )
    next(gen)
    gen.close()
    assert backend.in_flight == 0


def test_parse_chat_sse_yields_deltas_and_usage_then_stops_at_done():
    lines = [
        ": comment",
        "",
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        'data: {"choices": [{"delta": {"content": "hel"}}]}',
        'data: {"choices": [{"delta": {"content": "lo"}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}',
        "data: [DONE]",
        'data: {"choices": [{"delta": {"content": "after done"}}]}',
    ]
    events = list(parse_chat_sse(iter(lines)))
    assert events == [
        ("content", "hel"),
        ("content", "lo"),
        ("usage", {"input_tokens": 7, "output_tokens": 2}),
    ]


def test_parse_chat_sse_refuses_tool_call_deltas_loudly():
    lines = [
        'data: {"choices": [{"delta": {"tool_calls": [{"id": "t1"}]}}]}',
    ]
    with pytest.raises(NotImplementedError, match="streamed tool calls"):
        list(parse_chat_sse(iter(lines)))


def test_a_client_disconnect_is_observed_as_an_outcome():
    backend = admitted("ollama")
    seen: list[dict] = []
    gen = relay(
        alias="qwen-local-1.5b",
        backend=backend,
        events=iter([("content", "a"), ("content", "b")]),
        source="override",
        fallback_chain=[],
        reopen=no_reopen,
        clock=make_clock(),
        observe=seen.append,
    )
    next(gen)
    gen.close()
    assert seen == [{"type": "disconnect", "backend": "ollama"}]
