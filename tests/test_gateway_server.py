"""The gateway server offline: fake clients behind the real app — source
recording, pin loudness, the fallback walk, admission, and the probe."""

from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from resgraph.gateway import server
from resgraph.gateway.server import create_app, probe

SETUPS = {
    "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "opus": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "extra_args": {"thinking": {"type": "adaptive"}},
    },
    "qwen-local-1.5b": {
        "provider": "ollama",
        "model": "qwen2.5:1.5b",
        "base_url": "http://localhost:11434/v1",
        "probe_interval_s": 60,
    },
}


class FakeClient:
    def __init__(self, name: str, behaviors: dict[str, str], calls: list[tuple[str, dict]]):
        self.name = name
        self.behaviors = behaviors
        self.calls = calls
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append((self.name, kwargs))
        if self.behaviors.get(self.name) == "boom":
            raise ConnectionError("backend unreachable")
        return SimpleNamespace(
            content=[{"type": "text", "text": f"from {self.name}"}],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


@pytest.fixture
def harness(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    calls: list[tuple[str, dict]] = []
    behaviors: dict[str, str] = {}
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], behaviors, calls),
    )
    return TestClient(app), behaviors, calls


def _gen(client: TestClient, **body: Any):
    return client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "hi"}], **body}
    )


def test_task_class_serves_and_records_source_and_backend(harness):
    client, _, _ = harness
    r = _gen(client, task_class="judgment")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "haiku"
    assert body["source"] == "task_class_default"
    assert body["backend"] == "anthropic"
    assert body["fallback_chain"] == []
    assert body["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_a_pin_fails_loudly_and_never_substitutes(harness):
    client, behaviors, calls = harness
    behaviors["haiku"] = "boom"
    r = _gen(client, pin="haiku")
    assert r.status_code == 502
    assert [name for name, _ in calls] == ["haiku"]


def test_init_failure_walks_to_the_other_backend(harness):
    client, behaviors, _ = harness
    behaviors["haiku"] = "boom"
    r = _gen(client, task_class="judgment")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "qwen-local-1.5b"
    assert body["backend"] == "ollama"
    assert body["fallback_chain"] == ["anthropic:haiku"]
    assert body["source"] == "task_class_default"


def test_an_exhausted_walk_is_a_clean_503(harness):
    client, behaviors, _ = harness
    behaviors["haiku"] = "boom"
    behaviors["qwen-local-1.5b"] = "boom"
    r = _gen(client, task_class="judgment")
    assert r.status_code == 503


def test_an_unknown_alias_is_a_400(harness):
    client, _, _ = harness
    assert _gen(client, model="nope").status_code == 400


def test_an_unknown_task_class_is_a_422(harness):
    client, _, _ = harness
    assert _gen(client, task_class="mystery").status_code == 422


def test_a_full_queue_answers_429_with_retry_after(harness):
    client, _, _ = harness
    gw = client.app.state.gateway
    gw.backend("qwen-local-1.5b").in_flight = 4
    r = _gen(client, model="qwen-local-1.5b")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_anthropic_extra_args_ride_the_create_call(harness):
    client, _, calls = harness
    r = _gen(client, pin="opus")
    assert r.status_code == 200
    _, kwargs = calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}


def test_probe_classifies_ok_and_fail(harness, monkeypatch):
    client, behaviors, _ = harness
    gw = client.app.state.gateway
    assert probe(gw, "haiku") == "ok"
    behaviors["haiku"] = "boom"
    assert probe(gw, "haiku") == "fail"


def test_probe_classifies_slow(harness, monkeypatch):
    client, _, _ = harness
    monkeypatch.setattr(server, "PROBE_SLOW_S", 0.0)
    assert probe(client.app.state.gateway, "haiku") == "slow"


def test_object_blocks_translate_to_wire_dicts(harness, monkeypatch):
    from resgraph.evals.providers import TextBlock, ToolUseBlock

    client, _, _ = harness

    def create(**kwargs):
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="weighing the config change"),
                TextBlock(text="thinking done"),
                ToolUseBlock(id="t1", name="fetch_resource", input={"id": "srv-1"}),
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
        )

    gw = client.app.state.gateway
    monkeypatch.setattr(gw.client("haiku"), "create", create)
    r = _gen(client, task_class="judgment")
    assert r.status_code == 200
    assert r.json()["content"] == [
        {"type": "thinking", "thinking": "weighing the config change"},
        {"type": "text", "text": "thinking done"},
        {"type": "tool_use", "id": "t1", "name": "fetch_resource", "input": {"id": "srv-1"}},
    ]


def test_system_and_tools_ride_the_create_call(harness):
    client, _, calls = harness
    tools = [{"name": "fetch_resource", "input_schema": {}}]
    r = _gen(client, task_class="judgment", system="be brief", tools=tools)
    assert r.status_code == 200
    _, kwargs = calls[0]
    assert kwargs["system"] == "be brief"
    assert kwargs["tools"] == tools


def test_the_walk_never_reaches_an_unrouted_catalog_setup(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump({**SETUPS, "gpt": {"provider": "openai", "model": "gpt-4o"}}))
    calls: list[tuple[str, dict]] = []
    behaviors = {"haiku": "boom", "qwen-local-1.5b": "boom"}
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], behaviors, calls),
    )
    client = TestClient(app)
    r = _gen(client, task_class="judgment")
    assert r.status_code == 503
    assert "gpt" not in [name for name, _ in calls]
    assert app.state.gateway.alias_for_backend("openai") is None
    assert app.state.gateway.alias_for_backend("nowhere") is None


def sse_events(text: str) -> list[dict]:
    import json

    return [
        json.loads(line[len("data:") :]) for line in text.splitlines() if line.startswith("data:")
    ]


def ok_stream(alias: str, kwargs: Any):
    return iter(
        [
            ("content", f"from {alias}"),
            ("usage", {"input_tokens": 2, "output_tokens": 1}),
        ]
    )


def stream_harness(tmp_path, factory):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    calls: list[tuple[str, dict]] = []
    behaviors: dict[str, str] = {}
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], behaviors, calls),
        stream_factory=factory,
    )
    return TestClient(app)


def test_a_stream_serves_end_to_end(tmp_path):
    client = stream_harness(tmp_path, ok_stream)
    r = _gen(client, model="qwen-local-1.5b", stream=True)
    assert r.status_code == 200
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["content", "end"]
    end = events[-1]
    assert end["model"] == "qwen-local-1.5b"
    assert end["source"] == "override"
    assert end["backend"] == "ollama"
    assert end["reconciliation_ok"] is True


def test_a_stream_open_failure_walks_like_an_init_failure(tmp_path):
    def factory(alias: str, kwargs: Any):
        if alias == "qwen-local-1.5b":
            raise ConnectionError("backend unreachable")
        return ok_stream(alias, kwargs)

    client = stream_harness(tmp_path, factory)
    r = _gen(client, task_class="workhorse", stream=True)
    assert r.status_code == 200
    end = sse_events(r.text)[-1]
    assert end["backend"] == "anthropic"
    assert end["fallback_chain"] == ["ollama:qwen-local-1.5b"]


def test_a_zero_token_stream_death_restarts_on_the_other_backend(tmp_path):
    def immediately_dying(alias: str, kwargs: Any):
        def gen():
            raise ConnectionError("died before any token")
            yield  # pragma: no cover

        return gen()

    def factory(alias: str, kwargs: Any):
        if alias == "qwen-local-1.5b":
            return immediately_dying(alias, kwargs)
        return ok_stream(alias, kwargs)

    client = stream_harness(tmp_path, factory)
    r = _gen(client, task_class="workhorse", stream=True)
    assert r.status_code == 200
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["content", "end"]
    assert events[-1]["fallback_chain"] == ["ollama:qwen-local-1.5b"]
    assert events[-1]["backend"] == "anthropic"


def test_a_pinned_stream_open_failure_is_a_loud_502(tmp_path):
    def factory(alias: str, kwargs: Any):
        raise ConnectionError("backend unreachable")

    client = stream_harness(tmp_path, factory)
    r = _gen(client, pin="qwen-local-1.5b", stream=True)
    assert r.status_code == 502


def test_a_full_queue_rejects_a_stream_with_retry_after(tmp_path):
    client = stream_harness(tmp_path, ok_stream)
    client.app.state.gateway.backend("qwen-local-1.5b").in_flight = 4
    r = _gen(client, pin="qwen-local-1.5b", stream=True)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_anthropic_streaming_answers_501_until_its_adapter_lands(harness):
    client, _, _ = harness
    r = _gen(client, task_class="judgment", stream=True)
    assert r.status_code == 501


def test_the_default_stream_factory_routes_through_the_seam(tmp_path):
    from resgraph.evals.providers import ChatCompletionsClient

    captured: dict[str, Any] = {}

    def fake_lines(url: str, payload: dict, headers: dict):
        captured.update(url=url, payload=payload, headers=headers)
        yield 'data: {"choices": [{"delta": {"content": "pong"}}]}'
        yield 'data: {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}'
        yield "data: [DONE]"

    def factory(setup: dict[str, Any]) -> ChatCompletionsClient:
        return ChatCompletionsClient(
            base_url=setup["base_url"],
            extra_args={"guided_json": {"type": "object"}},
            line_transport=fake_lines,
        )

    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    app = create_app(models_path=path, client_factory=factory)
    client = TestClient(app)
    r = _gen(client, model="qwen-local-1.5b", stream=True)
    assert r.status_code == 200
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["content", "end"]
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert captured["payload"]["model"] == "qwen2.5:1.5b"
    assert captured["payload"]["guided_json"] == {"type": "object"}
    assert captured["url"].endswith("/chat/completions")


def test_a_pinned_stream_never_restarts_after_a_zero_token_death(tmp_path):
    opened: list[str] = []

    def factory(alias: str, kwargs: Any):
        opened.append(alias)

        def gen():
            raise ConnectionError("died at zero tokens")
            yield  # pragma: no cover

        return gen()

    client = stream_harness(tmp_path, factory)
    r = _gen(client, pin="qwen-local-1.5b", stream=True)
    assert r.status_code == 200
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["stream_error"]
    assert events[0]["tokens_emitted"] == 0
    assert opened == ["qwen-local-1.5b"]


def test_a_failed_reopen_walks_on_to_exhaustion(tmp_path):
    opened: list[str] = []

    def factory(alias: str, kwargs: Any):
        opened.append(alias)
        if alias == "qwen-local-1.5b":

            def gen():
                raise ConnectionError("zero-token death")
                yield  # pragma: no cover

            return gen()
        raise ConnectionError("open refused")

    client = stream_harness(tmp_path, factory)
    r = _gen(client, task_class="workhorse", stream=True)
    assert r.status_code == 200
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["stream_error"]
    assert events[0]["tokens_emitted"] == 0
    assert opened == ["qwen-local-1.5b", "haiku"]


def test_both_request_shapes_feed_the_same_ttft_series(tmp_path, monkeypatch):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    calls: list[tuple[str, dict]] = []
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], {}, calls),
        stream_factory=ok_stream,
    )
    client = TestClient(app)
    window = client.app.state.gateway.backend("qwen-local-1.5b").ttft
    samples: list[float] = []
    original = window.observe
    monkeypatch.setattr(
        window, "observe", lambda v, now=None: (samples.append(v), original(v, now))[1]
    )

    assert _gen(client, model="qwen-local-1.5b").status_code == 200
    assert _gen(client, model="qwen-local-1.5b", stream=True).status_code == 200
    # One sample per request shape, same series: dispatch ranks streamed and
    # non-streamed traffic on one rolling percentile view per backend.
    assert len(samples) == 2
    assert window.percentile(50) is not None


def test_a_probe_round_probes_only_declared_setups(harness, caplog):
    import logging as _logging

    from resgraph.gateway.server import run_probe_round

    client, behaviors, calls = harness
    gw = client.app.state.gateway
    with caplog.at_level(_logging.WARNING, logger="resgraph.gateway"):
        results = run_probe_round(gw, now=0.0)
    assert results == {"ollama": "ok"}
    assert "haiku" not in [name for name, _ in calls]
    assert not caplog.records

    behaviors["qwen-local-1.5b"] = "boom"
    with caplog.at_level(_logging.WARNING, logger="resgraph.gateway"):
        results = run_probe_round(gw, now=60.0)
    assert results["ollama"] == "fail"
    assert gw.backend("qwen-local-1.5b").health.state == "down"
    assert any("[gateway:health]" in r.message for r in caplog.records)


def test_probe_rounds_respect_the_per_setup_cadence(harness):
    from resgraph.gateway.server import probe_tick, run_probe_round

    client, _, calls = harness
    gw = client.app.state.gateway
    gw.setups["qwen-local-1.5b"]["probe_interval_s"] = 30
    assert probe_tick(gw) == 30
    assert run_probe_round(gw, now=0.0) == {"ollama": "ok"}
    assert run_probe_round(gw, now=10.0) == {}
    assert run_probe_round(gw, now=30.0) == {"ollama": "ok"}
    assert len(calls) == 2


def test_probe_tick_is_none_when_no_setup_declares_a_probe(tmp_path):
    from resgraph.gateway.server import probe_tick

    path = tmp_path / "models.yaml"
    undeclared = {
        k: {kk: vv for kk, vv in v.items() if kk != "probe_interval_s"} for k, v in SETUPS.items()
    }
    path.write_text(yaml.safe_dump(undeclared))
    app = create_app(models_path=path, client_factory=lambda setup: None)
    assert probe_tick(app.state.gateway) is None


def test_probe_rounds_readmit_gradually(harness):
    from resgraph.gateway.server import run_probe_round

    client, behaviors, _ = harness
    gw = client.app.state.gateway
    behaviors["qwen-local-1.5b"] = "boom"
    run_probe_round(gw, now=0.0)
    assert gw.backend("qwen-local-1.5b").health.state == "down"
    del behaviors["qwen-local-1.5b"]
    run_probe_round(gw, now=60.0)
    run_probe_round(gw, now=120.0)
    assert gw.backend("qwen-local-1.5b").health.state == "down"
    run_probe_round(gw, now=180.0)
    assert gw.backend("qwen-local-1.5b").health.state == "healthy"


def test_the_lifespan_probe_thread_runs_and_stops(tmp_path):
    import time as _time

    path = tmp_path / "models.yaml"
    setups = {k: dict(v) for k, v in SETUPS.items()}
    setups["qwen-local-1.5b"]["probe_interval_s"] = 0.01
    path.write_text(yaml.safe_dump(setups))
    behaviors = {"qwen-local-1.5b": "boom"}
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], behaviors, []),
    )
    with TestClient(app) as client:
        gw = client.app.state.gateway
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline:
            states = {b.health.state for b in gw.backends.values()}
            if states == {"down"}:
                break
            _time.sleep(0.02)
        assert {b.health.state for b in gw.backends.values()} == {"down"}


def test_probe_rounds_never_touch_an_unrouted_catalog_setup(tmp_path):
    from resgraph.gateway.server import run_probe_round

    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump({**SETUPS, "gpt": {"provider": "openai", "model": "gpt-4o"}}))
    calls: list[tuple[str, dict]] = []
    app = create_app(
        models_path=path, client_factory=lambda setup: FakeClient(setup["name"], {}, calls)
    )
    results = run_probe_round(TestClient(app).app.state.gateway)
    assert sorted(results) == ["ollama"]
    assert "gpt" not in [name for name, _ in calls]


def test_a_probe_is_a_minimal_generation_without_caller_extra_args(harness):
    client, _, calls = harness
    assert probe(client.app.state.gateway, "opus") == "ok"
    name, kwargs = calls[-1]
    assert name == "opus"
    assert kwargs["max_tokens"] == 5
    assert "thinking" not in kwargs


class CacheUsageFake:
    """Dumb on purpose: fixed cache usage, every request recorded. The
    assertions carry the proof — a fake smart enough to be wrong would
    need tests of its own."""

    def __init__(self, received: list[dict]):
        self.received = received
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.received.append(kwargs)
        return SimpleNamespace(
            content=[{"type": "text", "text": "ok"}],
            usage=SimpleNamespace(
                input_tokens=3,
                output_tokens=1,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=25,
            ),
        )


ANALYST_SHAPED_BODY = {
    "system": [
        {"type": "text", "text": "the playbook prefix", "cache_control": {"type": "ephemeral"}}
    ],
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "triage this", "cache_control": {"type": "ephemeral"}}
            ],
        }
    ],
    "tools": [{"name": "fetch_resource", "input_schema": {"type": "object"}}],
    "pin": "haiku",
}


def caching_harness(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    received: list[dict] = []
    app = create_app(models_path=path, client_factory=lambda setup: CacheUsageFake(received))
    return TestClient(app), received


def test_cache_usage_fields_pass_through_the_hop(tmp_path):
    client, _ = caching_harness(tmp_path)
    r = client.post("/v1/generate", json=dict(ANALYST_SHAPED_BODY))
    assert r.status_code == 200
    assert r.json()["usage"]["cache_read_tokens"] == 900
    assert r.json()["usage"]["cache_creation_tokens"] == 25


def test_identical_requests_are_forwarded_byte_identically(tmp_path):
    client, received = caching_harness(tmp_path)
    assert client.post("/v1/generate", json=dict(ANALYST_SHAPED_BODY)).status_code == 200
    assert client.post("/v1/generate", json=dict(ANALYST_SHAPED_BODY)).status_code == 200
    assert received[0] == received[1]


def test_cache_control_marks_survive_the_hop_untouched(tmp_path):
    client, received = caching_harness(tmp_path)
    assert client.post("/v1/generate", json=dict(ANALYST_SHAPED_BODY)).status_code == 200
    kwargs = received[0]
    assert kwargs["system"] == ANALYST_SHAPED_BODY["system"]
    assert kwargs["messages"] == ANALYST_SHAPED_BODY["messages"]
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_the_analysts_block_system_is_accepted_not_422d(harness):
    client, _, calls = harness
    system = [
        {"type": "text", "text": "the playbook prefix", "cache_control": {"type": "ephemeral"}}
    ]
    r = _gen(client, task_class="judgment", system=system)
    assert r.status_code == 200
    _, kwargs = calls[0]
    assert kwargs["system"] == system


def test_a_stream_to_a_down_backend_with_no_streamable_fallback_is_an_eager_503(harness):
    """The INC-004 hot loop: with the default factory anthropic cannot
    stream, so a down local backend refuses at admission with Retry-After
    instead of an instant stream_error the client will hammer."""
    client, _, calls = harness
    client.app.state.gateway.backend("qwen-local-1.5b").health.state = "down"
    r = _gen(client, task_class="workhorse", stream=True)
    assert r.status_code == 503
    assert int(r.headers["Retry-After"]) >= 1
    assert calls == []


def test_a_pinned_stream_to_a_down_backend_is_an_eager_502(harness):
    client, _, calls = harness
    client.app.state.gateway.backend("qwen-local-1.5b").health.state = "down"
    r = _gen(client, pin="qwen-local-1.5b", stream=True)
    assert r.status_code == 502
    assert calls == []


def test_a_down_backend_with_a_streamable_fallback_still_walks(tmp_path):
    def factory(alias: str, kwargs: Any):
        if alias == "qwen-local-1.5b":
            raise ConnectionError("backend unreachable")
        return ok_stream(alias, kwargs)

    client = stream_harness(tmp_path, factory)
    client.app.state.gateway.backend("qwen-local-1.5b").health.state = "down"
    r = _gen(client, task_class="workhorse", stream=True)
    assert r.status_code == 200
    end = sse_events(r.text)[-1]
    assert end["backend"] == "anthropic"
    assert end["fallback_chain"] == ["ollama:qwen-local-1.5b"]


def test_a_non_positive_probe_cadence_is_refused_at_startup(tmp_path):
    path = tmp_path / "models.yaml"
    setups = {k: dict(v) for k, v in SETUPS.items()}
    setups["qwen-local-1.5b"]["probe_interval_s"] = 0
    path.write_text(yaml.safe_dump(setups))
    with pytest.raises(SystemExit, match="must be > 0"):
        create_app(models_path=path, client_factory=lambda setup: None)


def test_ignore_probes_suppresses_a_declared_cadence(tmp_path):
    import time as _time

    path = tmp_path / "models.yaml"
    setups = {k: dict(v) for k, v in SETUPS.items()}
    setups["qwen-local-1.5b"]["probe_interval_s"] = 0.01
    path.write_text(yaml.safe_dump(setups))
    calls: list[tuple[str, dict]] = []
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], {}, calls),
        ignore_probes=True,
    )
    with TestClient(app):
        _time.sleep(0.05)
    assert calls == []
