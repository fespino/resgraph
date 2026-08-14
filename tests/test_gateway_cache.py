"""The response cache offline: identity, TTL, LRU, counters — and through
the app: only deterministic non-streamed requests are served from cache,
and a hit says so."""

from types import SimpleNamespace
from typing import Any

import yaml
from fastapi.testclient import TestClient

from resgraph.gateway.cache import ResponseCache, cache_key
from resgraph.gateway.server import create_app

SETUPS = {
    "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "qwen-local-1.5b": {
        "provider": "ollama",
        "model": "qwen2.5:1.5b",
        "base_url": "http://localhost:11434/v1",
        "temperature": 0,
    },
}


class FakeClient:
    def __init__(self, name: str, calls: list[tuple[str, dict]]):
        self.name = name
        self.calls = calls
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append((self.name, kwargs))
        return SimpleNamespace(
            content=[{"type": "text", "text": f"from {self.name}"}],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def _gen(client: TestClient, **body: Any):
    return client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "hi"}], **body}
    )


def ticking(step: float = 1.0):
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += step
        return state["t"]

    return clock


def test_a_one_token_difference_is_a_different_resource():
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    changed = {"model": "m", "messages": [{"role": "user", "content": "hi!"}]}
    assert cache_key("a", base) != cache_key("a", changed)
    assert cache_key("a", base) != cache_key("b", base)
    assert cache_key("a", base) == cache_key("a", dict(base))


def test_ttl_expires_entries():
    c = ResponseCache(ttl_s=10.0, clock=ticking(step=6.0))
    c.put("k", "v", tokens=5)
    assert c.get("k") == "v"
    assert c.get("k") is None
    assert (c.hits, c.misses) == (1, 1)


def test_lru_evicts_the_coldest_and_a_hit_refreshes_recency():
    c = ResponseCache(max_entries=2, clock=ticking())
    c.put("a", 1, tokens=1)
    c.put("b", 2, tokens=1)
    assert c.get("a") == 1
    c.put("c", 3, tokens=1)
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3


def test_tokens_saved_accumulates_on_hits_only():
    c = ResponseCache(clock=ticking())
    c.put("k", "v", tokens=40)
    assert c.tokens_saved == 0
    c.get("k")
    c.get("k")
    assert c.tokens_saved == 80


def cached_harness(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    calls: list[tuple[str, dict]] = []
    app = create_app(
        models_path=path, client_factory=lambda setup: FakeClient(setup["name"], calls)
    )
    return TestClient(app), calls


def test_a_deterministic_repeat_is_served_from_cache_and_says_so(tmp_path):
    client, calls = cached_harness(tmp_path)
    first = _gen(client, model="qwen-local-1.5b")
    second = _gen(client, model="qwen-local-1.5b")
    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert second.json()["usage"] == first.json()["usage"]
    assert len(calls) == 1


def test_a_sampled_setup_is_never_served_from_cache(tmp_path):
    client, calls = cached_harness(tmp_path)
    _gen(client, task_class="judgment")
    r = _gen(client, task_class="judgment")
    assert r.json()["cached"] is False
    assert len(calls) == 2


def test_a_different_request_misses(tmp_path):
    client, calls = cached_harness(tmp_path)
    _gen(client, model="qwen-local-1.5b")
    r = _gen(client, model="qwen-local-1.5b", max_tokens=99)
    assert r.json()["cached"] is False
    assert len(calls) == 2


def test_a_stream_is_never_served_from_cache(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    opened: list[str] = []

    def factory(alias: str, kwargs: Any):
        opened.append(alias)
        return iter([("content", "x"), ("usage", {"input_tokens": 1, "output_tokens": 1})])

    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], []),
        stream_factory=factory,
    )
    client = TestClient(app)
    _gen(client, model="qwen-local-1.5b")
    r = _gen(client, model="qwen-local-1.5b", stream=True)
    assert r.status_code == 200
    assert opened == ["qwen-local-1.5b"]


def test_no_cache_bypasses_read_and_write(tmp_path):
    # A measured run must see the distribution, not a replay: k identical
    # trials served from cache would silently collapse pass^k into pass@1.
    client, calls = cached_harness(tmp_path)
    first = _gen(client, model="qwen-local-1.5b", no_cache=True)
    second = _gen(client, model="qwen-local-1.5b", no_cache=True)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is False
    assert len(calls) == 2
    third = _gen(client, model="qwen-local-1.5b")
    assert third.json()["cached"] is False
    assert len(calls) == 3
