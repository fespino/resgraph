"""The catalog primitive (D40): one alias, many endpoints; /v1/models;
capability admission. The alias is the request vocabulary, the endpoint
is the routable unit."""

import pytest
import yaml
from fastapi.testclient import TestClient

from resgraph.evals.providers import load_setup
from resgraph.gateway import server
from resgraph.gateway.registry import capability_mismatch, expand
from resgraph.gateway.router import ClassRoute

TWO_ENDPOINTS = {
    "qwen": {
        "model": "qwen2.5:1.5b",
        "temperature": 0,
        "context_window": 8192,
        "capabilities": {"tools": True},
        "endpoints": [
            {"name": "ollama", "provider": "ollama", "base_url": "http://a", "quant": "Q4_K_M"},
            {"name": "llamacpp", "provider": "llamacpp", "base_url": "http://b", "quant": "Q8_0"},
        ],
    },
    "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
}


def test_expand_merges_endpoint_over_parent():
    table, aliases = expand(TWO_ENDPOINTS)
    assert aliases["qwen"] == ["qwen@ollama", "qwen@llamacpp"]
    assert table["qwen@ollama"]["model"] == "qwen2.5:1.5b"  # inherited
    assert table["qwen@ollama"]["quant"] == "Q4_K_M"  # endpoint's own
    assert "endpoints" not in table["qwen@ollama"] and "name" not in table["qwen@ollama"]
    assert aliases["haiku"] == ["haiku"] and table["haiku"] is TWO_ENDPOINTS["haiku"]


def test_expand_rejects_bad_shapes():
    with pytest.raises(SystemExit, match="endpoints must not be empty"):
        expand({"a": {"endpoints": []}})
    with pytest.raises(SystemExit, match="needs a name"):
        expand({"a": {"endpoints": [{"provider": "x"}]}})
    with pytest.raises(SystemExit, match="duplicate endpoint name"):
        expand({"a": {"endpoints": [{"name": "e", "provider": "x"}, {"name": "e"}]}})
    with pytest.raises(SystemExit, match="reserved"):
        expand({"a@b": {}})


def test_capability_admission_filters_on_declared_only():
    declared = {"capabilities": {"tools": False}}
    assert "tools: false" in str(capability_mismatch(declared, wants_tools=True, max_tokens=10))
    assert capability_mismatch(declared, wants_tools=False, max_tokens=10) is None
    assert capability_mismatch({}, wants_tools=True, max_tokens=10) is None  # undeclared admits
    windowed = {"context_window": 100}
    assert "context_window" in str(capability_mismatch(windowed, wants_tools=False, max_tokens=200))


class _Client:
    """One fake client per endpoint; failures configured by base_url."""

    def __init__(self, setup, failing):
        self.setup = setup
        self.failing = failing
        self.messages = self

    def create(self, **kwargs):
        if self.setup.get("base_url") in self.failing:
            raise ConnectionError(f"{self.setup['name']} down")
        return type(
            "R",
            (),
            {
                "content": [{"type": "text", "text": f"via {self.setup['name']}"}],
                "usage": type(
                    "U", (), {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}
                )(),
            },
        )()


def _app(tmp_path, setups, failing=frozenset(), registry=None):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(setups))
    return server.create_app(
        models_path=path,
        client_factory=lambda setup: _Client(setup, failing),
        registry=registry or {},
        ignore_probes=True,
    )


def test_selection_serves_one_of_the_alias_endpoints(tmp_path):
    app = _app(tmp_path, TWO_ENDPOINTS)
    out = TestClient(app).post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], "model": "qwen"}
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["model"] in ("qwen@ollama", "qwen@llamacpp")
    assert body["fallback_chain"] == []


def test_the_walk_hops_within_the_alias_before_cross_model(tmp_path):
    app = _app(tmp_path, TWO_ENDPOINTS, failing={"http://a"})
    out = TestClient(app).post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], "model": "qwen"}
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["model"] == "qwen@llamacpp"  # same model, second serving location
    assert body["fallback_chain"] == ["qwen@ollama:qwen@ollama"]


def test_a_pin_on_a_multi_endpoint_alias_is_ambiguous(tmp_path):
    client = TestClient(_app(tmp_path, TWO_ENDPOINTS))
    out = client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], "pin": "qwen"}
    )
    assert out.status_code == 400 and "ambiguous" in out.json()["detail"]
    pinned = client.post(
        "/v1/generate",
        json={"messages": [{"role": "user", "content": "x"}], "pin": "qwen@llamacpp"},
    )
    assert pinned.status_code == 200 and pinned.json()["model"] == "qwen@llamacpp"


def test_an_override_may_name_an_endpoint_id_directly(tmp_path):
    out = TestClient(_app(tmp_path, TWO_ENDPOINTS)).post(
        "/v1/generate",
        json={"messages": [{"role": "user", "content": "x"}], "model": "qwen@ollama"},
    )
    assert out.status_code == 200 and out.json()["model"] == "qwen@ollama"


def test_a_pin_refuses_on_capability_mismatch_and_on_unknown(tmp_path):
    client = TestClient(_app(tmp_path, TWO_ENDPOINTS))
    small = client.post(
        "/v1/generate",
        json={
            "messages": [{"role": "user", "content": "x"}],
            "pin": "qwen@ollama",
            "max_tokens": 9000,
        },
    )
    assert small.status_code == 400 and "cannot serve" in small.json()["detail"]
    ghost = client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], "pin": "ghost"}
    )
    assert ghost.status_code == 400 and "unknown" in ghost.json()["detail"]


def test_the_cross_model_walk_never_reattempts_a_tried_endpoint(tmp_path):
    """Both endpoints fail; the routed provider's serving endpoint is one
    of them, so the walk must exhaust at two hops, not loop back."""
    registry = {"workhorse": ClassRoute("qwen", "test route")}
    app = _app(tmp_path, TWO_ENDPOINTS, failing={"http://a", "http://b"}, registry=registry)
    out = TestClient(app).post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], "model": "qwen"}
    )
    assert out.status_code == 503
    # chain entries are backend:endpoint; one ":qwen@" per hop
    assert out.json()["detail"].count(":qwen@") == 2  # each endpoint tried exactly once


def test_capability_mismatch_refuses_with_the_reasons_named(tmp_path):
    setups = {
        "small": {
            "provider": "ollama",
            "model": "m",
            "base_url": "http://a",
            "context_window": 100,
        }
    }
    out = TestClient(_app(tmp_path, setups)).post(
        "/v1/generate",
        json={"messages": [{"role": "user", "content": "x"}], "model": "small", "max_tokens": 500},
    )
    assert out.status_code == 400
    assert "context_window" in out.json()["detail"]


def test_v1_models_serves_the_catalog_from_the_registry(tmp_path):
    registry = {"workhorse": ClassRoute("qwen", "test route")}
    app = _app(tmp_path, TWO_ENDPOINTS, registry=registry)
    data = {row["alias"]: row for row in TestClient(app).get("/v1/models").json()["data"]}
    assert set(data) == {"qwen", "haiku"}
    assert data["qwen"]["routed"] is True and data["haiku"]["routed"] is False
    ids = [e["id"] for e in data["qwen"]["endpoints"]]
    assert ids == ["qwen@ollama", "qwen@llamacpp"]
    assert data["qwen"]["endpoints"][0]["quant"] == "Q4_K_M"
    assert data["qwen"]["endpoints"][0]["pricing"] is None  # no price on file: unmetered
    haiku_price = data["haiku"]["endpoints"][0]["pricing"]
    assert haiku_price == {"input_per_mtok": 1.0, "output_per_mtok": 5.0}


def test_load_setup_resolves_endpoints_and_refuses_ambiguity(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(TWO_ENDPOINTS))
    setup = load_setup("qwen@llamacpp", path)
    assert setup["quant"] == "Q8_0" and setup["model"] == "qwen2.5:1.5b"
    assert load_setup("haiku", path)["provider"] == "anthropic"
    with pytest.raises(SystemExit, match="name one"):
        load_setup("qwen", path)
