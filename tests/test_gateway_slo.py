"""Gateway SLO wiring: the dashboard panels reference recording rules that
exist, and the gateway instruments record without a provider installed
(no-op) so instrumentation never crashes a request."""

import json
from pathlib import Path

import yaml

from resgraph import obs

RULES = Path("observability/rules/gateway_slo.yml")
DASHBOARD = Path("observability/grafana/dashboards/resgraph-overview.json")


def recorded_rules() -> set[str]:
    doc = yaml.safe_load(RULES.read_text())
    return {r["record"] for g in doc["groups"] for r in g["rules"] if "record" in r}


def test_gateway_panels_reference_real_rules():
    recorded = recorded_rules()
    dashboard = json.loads(DASHBOARD.read_text())
    referenced = {
        t["expr"]
        for p in dashboard["panels"]
        for t in p.get("targets", [])
        if t["expr"].startswith("slo:gateway_")
    }
    assert referenced, "the dashboard should carry the gateway SLO panels"
    assert referenced <= recorded, f"panels reference undefined rules: {referenced - recorded}"


def test_gateway_instruments_record_without_a_provider():
    obs.GATEWAY_TTFT.record(0.4, {"backend": "ollama", "cached": "false"})
    obs.GATEWAY_TOKENS_PER_S.record(12.0, {"backend": "ollama"})
    obs.GATEWAY_REQUESTS.add(
        1, {"backend": "ollama", "outcome": "ok", "source": "override", "task_class": "none"}
    )
    obs.GATEWAY_FALLBACK_CHAIN.record(0)
    obs.GATEWAY_STREAM_ERRORS.add(1, {"tokens_bucket": "zero"})
    obs.GATEWAY_CACHE_HITS.add(1, {"layer": "gateway"})
    obs.GATEWAY_CACHE_TOKENS_SAVED.add(15)
    obs.GATEWAY_COST.record(
        0.002, {"task_class": "judgment", "backend": "anthropic", "source": "pin"}
    )


def test_depth_reader_registration_survives_a_broken_reader():
    obs.register_gateway_depth_reader(lambda: (_ for _ in ()).throw(RuntimeError))
    assert list(obs._observe_gateway_depth(None)) == []
    obs.register_gateway_depth_reader(lambda: [("ollama", 2)])
    observations = list(obs._observe_gateway_depth(None))
    assert len(observations) == 1


def test_the_server_records_outcomes_costs_and_cache_hits(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from typing import Any

    from fastapi.testclient import TestClient

    from resgraph.gateway.server import create_app

    recorded: list[tuple[str, Any, dict]] = []

    class Rec:
        def __init__(self, name: str):
            self.name = name

        def add(self, value, labels=None):
            recorded.append((self.name, value, labels or {}))

        def record(self, value, labels=None):
            recorded.append((self.name, value, labels or {}))

    from resgraph.gateway import server as gwserver

    path = tmp_path / "models.yaml"
    path.write_text(
        "haiku:\n  provider: anthropic\n  model: claude-haiku-4-5\n"
        "qwen-local-1.5b:\n  provider: ollama\n  model: qwen2.5:1.5b\n"
        "  base_url: http://localhost:11434/v1\n  temperature: 0\n"
    )

    class Fake:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            return SimpleNamespace(
                content=[{"type": "text", "text": "ok"}],
                usage=SimpleNamespace(
                    input_tokens=1000,
                    output_tokens=100,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

    client = TestClient(create_app(models_path=path, client_factory=lambda s: Fake()))
    # patch AFTER create_app: init_metrics rebinds the instrument globals
    for name in (
        "GATEWAY_TTFT",
        "GATEWAY_REQUESTS",
        "GATEWAY_FALLBACK_CHAIN",
        "GATEWAY_COST",
        "GATEWAY_CACHE_HITS",
        "GATEWAY_CACHE_TOKENS_SAVED",
    ):
        monkeypatch.setattr(gwserver.obs, name, Rec(name))
    body = {"messages": [{"role": "user", "content": "hi"}]}

    r = client.post("/v1/generate", json={**body, "task_class": "judgment"})
    assert r.status_code == 200
    outcomes = [(v, la) for n, v, la in recorded if n == "GATEWAY_REQUESTS"]
    assert outcomes[-1][1]["outcome"] == "ok"
    assert outcomes[-1][1]["task_class"] == "judgment"
    costs = [(v, la) for n, v, la in recorded if n == "GATEWAY_COST"]
    # 1000 in + 100 out on haiku pricing (1, 5 $/MTok) = 0.0015
    assert costs[-1][0] == 0.0015
    assert costs[-1][1] == {
        "backend": "anthropic",
        "source": "task_class_default",
        "task_class": "judgment",
    }

    client.post("/v1/generate", json={**body, "model": "qwen-local-1.5b"})
    client.post("/v1/generate", json={**body, "model": "qwen-local-1.5b"})
    hits = [(v, la) for n, v, la in recorded if n == "GATEWAY_CACHE_HITS"]
    assert hits[-1][1] == {"layer": "gateway"}
    saved = [(v, la) for n, v, la in recorded if n == "GATEWAY_CACHE_TOKENS_SAVED"]
    assert saved[-1][0] == 1100
    cached_outcomes = [
        la for n, v, la in recorded if n == "GATEWAY_REQUESTS" and la.get("outcome") == "cached"
    ]
    assert len(cached_outcomes) == 1


def test_the_stream_observer_maps_every_terminal_payload(tmp_path, monkeypatch):
    from resgraph.gateway import server as gwserver
    from resgraph.gateway.server import Gateway, _stream_observer

    recorded: list[tuple[str, object, dict]] = []

    class Rec:
        def __init__(self, name: str):
            self.name = name

        def add(self, value, labels=None):
            recorded.append((self.name, value, labels or {}))

        def record(self, value, labels=None):
            recorded.append((self.name, value, labels or {}))

    for name in (
        "GATEWAY_TTFT",
        "GATEWAY_TOKENS_PER_S",
        "GATEWAY_REQUESTS",
        "GATEWAY_FALLBACK_CHAIN",
        "GATEWAY_COST",
        "GATEWAY_STREAM_ERRORS",
    ):
        monkeypatch.setattr(gwserver.obs, name, Rec(name))

    gw = Gateway(
        setups={"qwen-local-1.5b": {"provider": "ollama", "model": "qwen2.5:1.5b"}},
        client_factory=lambda s: None,
    )
    observe = _stream_observer(gw, "override", "workhorse")

    observe(
        {
            "type": "end",
            "model": "qwen-local-1.5b",
            "source": "override",
            "backend": "ollama",
            "fallback_chain": [],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "ttft_s": 0.2,
            "tokens_per_s": 15.0,
            "reconciliation_ok": True,
        }
    )
    observe(
        {
            "type": "stream_error",
            "backend": "ollama",
            "reason": "died",
            "tokens_emitted": 4,
            "fallback_chain": ["ollama:qwen-local-1.5b", "anthropic:haiku"],
        }
    )
    observe({"type": "disconnect", "backend": "ollama"})

    outcomes = [la["outcome"] for n, v, la in recorded if n == "GATEWAY_REQUESTS"]
    assert outcomes == ["stream_ok", "stream_error", "client_disconnected"]
    disconnect = [la for n, v, la in recorded if n == "GATEWAY_REQUESTS"][-1]
    assert disconnect == {
        "backend": "ollama",
        "outcome": "client_disconnected",
        "source": "override",
        "task_class": "workhorse",
    }
    errors = [(v, la) for n, v, la in recorded if n == "GATEWAY_STREAM_ERRORS"]
    assert errors == [(1, {"tokens_bucket": "nonzero"})]
    chains = [v for n, v, la in recorded if n == "GATEWAY_FALLBACK_CHAIN"]
    assert chains == [0, 2]


def test_the_depth_gauge_is_quiet_before_any_gateway_registers(monkeypatch):
    monkeypatch.setattr(obs, "_gateway_depth_reader", None)
    assert list(obs._observe_gateway_depth(None)) == []


def test_both_cache_layers_emit_hits_and_misses(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from resgraph.gateway import server as gwserver
    from resgraph.gateway.server import create_app

    recorded: list[tuple[str, object, dict]] = []

    class Rec:
        def __init__(self, name: str):
            self.name = name

        def add(self, value, labels=None):
            recorded.append((self.name, value, labels or {}))

        def record(self, value, labels=None):
            recorded.append((self.name, value, labels or {}))

    path = tmp_path / "models.yaml"
    path.write_text(
        "haiku:\n  provider: anthropic\n  model: claude-haiku-4-5\n"
        "qwen-local-1.5b:\n  provider: ollama\n  model: qwen2.5:1.5b\n"
        "  base_url: http://localhost:11434/v1\n  temperature: 0\n"
    )

    class Fake:
        def __init__(self):
            self.messages = self
            self.warm = False

        def create(self, **kwargs):
            read = 800 if self.warm else 0
            creation = 0 if self.warm else 800
            self.warm = True
            return SimpleNamespace(
                content=[{"type": "text", "text": "ok"}],
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_input_tokens=read,
                    cache_creation_input_tokens=creation,
                ),
            )

    client = TestClient(create_app(models_path=path, client_factory=lambda s: Fake()))
    # patch AFTER create_app: init_metrics rebinds the instrument globals
    # on first install, which would clobber fakes patched before it.
    for name in ("GATEWAY_CACHE_HITS", "GATEWAY_CACHE_MISSES", "GATEWAY_TTFT"):
        monkeypatch.setattr(gwserver.obs, name, Rec(name))
    body = {"messages": [{"role": "user", "content": "hi"}]}

    # provider layer: cold write then warm read on anthropic
    client.post("/v1/generate", json={**body, "task_class": "judgment"})
    client.post("/v1/generate", json={**body, "task_class": "judgment"})
    provider = [(n, la) for n, v, la in recorded if la.get("layer") == "provider"]
    assert provider == [
        ("GATEWAY_CACHE_MISSES", {"layer": "provider"}),
        ("GATEWAY_CACHE_HITS", {"layer": "provider"}),
    ]
    warm_ttft = [la for n, v, la in recorded if n == "GATEWAY_TTFT"][-1]
    assert warm_ttft["cached"] == "true"

    # gateway layer: eligible miss then hit on the deterministic setup
    client.post("/v1/generate", json={**body, "model": "qwen-local-1.5b"})
    client.post("/v1/generate", json={**body, "model": "qwen-local-1.5b"})
    gateway = [(n, la) for n, v, la in recorded if la.get("layer") == "gateway"]
    assert gateway == [
        ("GATEWAY_CACHE_MISSES", {"layer": "gateway"}),
        ("GATEWAY_CACHE_HITS", {"layer": "gateway"}),
    ]


def test_failed_walks_record_their_chain_length(tmp_path, monkeypatch):
    """The INC-004 blind spot: a request that degrades through the whole
    walk and dies must still feed the chain histogram."""
    from fastapi.testclient import TestClient

    from resgraph.gateway import server as gwserver
    from resgraph.gateway.server import create_app

    recorded: list[float] = []

    class Rec:
        def record(self, value, labels=None):
            recorded.append(value)

    path = tmp_path / "models.yaml"
    path.write_text(
        "haiku:\n  provider: anthropic\n  model: claude-haiku-4-5\n"
        "qwen-local-1.5b:\n  provider: ollama\n  model: qwen2.5:1.5b\n"
        "  base_url: http://localhost:11434/v1\n  temperature: 0\n"
    )

    class Boom:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("dead backend")

    client = TestClient(create_app(models_path=path, client_factory=lambda s: Boom()))
    monkeypatch.setattr(gwserver.obs, "GATEWAY_FALLBACK_CHAIN", Rec())
    body = {"messages": [{"role": "user", "content": "hi"}]}

    r = client.post("/v1/generate", json={**body, "task_class": "judgment"})
    assert r.status_code == 503
    assert recorded == [2]

    recorded.clear()
    r = client.post("/v1/generate", json={**body, "pin": "haiku"})
    assert r.status_code == 502
    assert recorded == [0]
