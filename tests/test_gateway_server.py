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


def test_streaming_is_not_yet_served(harness):
    client, _, _ = harness
    assert _gen(client, task_class="judgment", stream=True).status_code == 501


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
