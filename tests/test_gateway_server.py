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
    assert body["usage"] == {"input_tokens": 10, "output_tokens": 5}


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


def test_fallback_reaches_a_provider_outside_the_registry(tmp_path):
    from resgraph.gateway.router import ClassRoute

    path = tmp_path / "models.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                "gpt": {"provider": "openai", "model": "gpt-4o"},
            }
        )
    )
    calls: list[tuple[str, dict]] = []
    behaviors = {"haiku": "boom"}
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], behaviors, calls),
        registry={"judgment": ClassRoute("haiku", "the only routed class")},
    )
    client = TestClient(app)
    r = _gen(client, task_class="judgment")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "gpt"
    assert body["backend"] == "openai"
    assert body["fallback_chain"] == ["anthropic:haiku"]
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


def test_a_failed_reopen_walks_on_and_exhausts_honestly(tmp_path):
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
    ewma = client.app.state.gateway.backend("qwen-local-1.5b").ttft_ewma
    samples: list[float] = []
    original = ewma.update
    monkeypatch.setattr(ewma, "update", lambda s: (samples.append(s), original(s))[1])

    assert _gen(client, model="qwen-local-1.5b").status_code == 200
    assert _gen(client, model="qwen-local-1.5b", stream=True).status_code == 200
    # One sample per request shape, same series: dispatch ranks streamed and
    # non-streamed traffic on one recency-weighted view per backend.
    assert len(samples) == 2
    assert ewma.value is not None
