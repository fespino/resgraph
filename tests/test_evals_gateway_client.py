"""The seam's gateway client: routing belongs to the setup, the winning
source rides back, and the whole path proves out offline against the real
gateway app."""

from types import SimpleNamespace
from typing import Any

import yaml
from fastapi.testclient import TestClient

from resgraph.evals.providers import GatewayClient, ThinkingBlock, ToolUseBlock, build_client
from resgraph.gateway.server import create_app


def test_the_setup_routes_and_the_body_carries_the_bypass():
    seen: dict[str, Any] = {}

    def transport(url, payload, headers):
        seen.update(url=url, payload=payload)
        return {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "source": "pin",
            "backend": "anthropic",
            "cached": False,
        }

    client = GatewayClient(
        base_url="http://gw:8080", pin="haiku", cache_responses=False, transport=transport
    )
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "adaptive"},
    )
    assert seen["url"] == "http://gw:8080/v1/generate"
    assert seen["payload"]["pin"] == "haiku"
    assert seen["payload"]["cache_responses"] is False
    assert "model" not in seen["payload"]
    assert "thinking" not in seen["payload"]
    assert resp.source == "pin"


def test_wire_blocks_round_trip_including_thinking_and_tools():
    def transport(url, payload, headers):
        return {
            "content": [
                {"type": "thinking", "thinking": "chain of reasoning"},
                {"type": "text", "text": "done"},
                {"type": "tool_use", "id": "t1", "name": "fetch_resource", "input": {"id": "x"}},
            ],
            "usage": {
                "input_tokens": 9,
                "output_tokens": 4,
                "cache_read_tokens": 800,
                "cache_creation_tokens": 0,
            },
            "source": "task_class_default",
            "backend": "anthropic",
            "cached": False,
        }

    client = GatewayClient(base_url="http://gw:8080", task_class="judgment", transport=transport)
    resp = client.messages.create(max_tokens=64, messages=[{"role": "user", "content": "q"}])
    kinds = [type(b) for b in resp.content]
    assert kinds == [ThinkingBlock, type(resp.content[1]), ToolUseBlock]
    assert resp.content[0].thinking == "chain of reasoning"
    assert resp.usage.cache_read_input_tokens == 800


def test_end_to_end_through_the_real_gateway_app(tmp_path):
    setups = {
        "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        "qwen-local-1.5b": {
            "provider": "ollama",
            "model": "qwen2.5:1.5b",
            "base_url": "http://localhost:11434/v1",
        },
    }
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(setups))

    class Fake:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            return SimpleNamespace(
                content=[{"type": "text", "text": "served"}],
                usage=SimpleNamespace(input_tokens=2, output_tokens=1),
            )

    app_client = TestClient(create_app(models_path=path, client_factory=lambda s: Fake()))

    def asgi_transport(url, payload, headers):
        r = app_client.post("/v1/generate", json=payload)
        assert r.status_code == 200, r.text
        return r.json()

    seam_client = GatewayClient(
        base_url="http://gateway", pin="haiku", cache_responses=False, transport=asgi_transport
    )
    resp = seam_client.messages.create(
        max_tokens=64,
        messages=[{"role": "user", "content": "triage this"}],
        system=[{"type": "text", "text": "prefix", "cache_control": {"type": "ephemeral"}}],
        tools=[{"name": "fetch_resource", "input_schema": {}}],
    )
    assert resp.content[0].text == "served"
    assert resp.source == "pin"
    assert resp.backend == "anthropic"
    assert resp.cached is False


def test_build_client_resolves_the_gateway_provider():
    client = build_client(
        {
            "name": "haiku-via-gateway",
            "provider": "gateway",
            "base_url": "http://gw:8080",
            "pin": "haiku",
        }
    )
    assert isinstance(client, GatewayClient)


def test_an_alias_setup_routes_as_a_model_override():
    seen: dict[str, Any] = {}

    def transport(url, payload, headers):
        seen.update(payload=payload)
        return {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "source": "override",
            "backend": "ollama",
        }

    client = GatewayClient(base_url="http://gw:8080", alias="qwen-local-1.5b", transport=transport)
    resp = client.messages.create(max_tokens=8, messages=[{"role": "user", "content": "hi"}])
    assert seen["payload"]["model"] == "qwen-local-1.5b"
    assert resp.source == "override"


def test_a_gateway_setup_without_a_base_url_fails_loudly():
    import pytest

    with pytest.raises(SystemExit, match="needs a base_url"):
        build_client({"name": "broken", "provider": "gateway"})
