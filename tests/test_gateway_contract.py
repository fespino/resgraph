"""The caller contract and the operator plane (D42): hard constraints
refuse loudly, soft preferences deprioritize, narrowing never broadens,
and a sort override disables the lottery entirely."""

import pytest
import yaml
from fastapi.testclient import TestClient

from resgraph.gateway import server
from resgraph.gateway.registry import load_policy

SETUPS = {
    "qwen": {
        "model": "qwen2.5:1.5b",
        "context_window": 8192,
        "endpoints": [
            {"name": "ollama", "provider": "ollama", "base_url": "http://a"},
            {
                "name": "hosted",
                "provider": "openai",
                "base_url": "http://b",
                "price_per_mtok": {"input": 1.0, "output": 2.0},
            },
        ],
    },
    "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
}


class _Client:
    def __init__(self, setup):
        self.setup = setup
        self.messages = self

    def create(self, **kwargs):
        return type(
            "R",
            (),
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": type(
                    "U", (), {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}
                )(),
            },
        )()


PRICED_PAIR = {
    "qwen": {
        "model": "qwen2.5:1.5b",
        "endpoints": [
            {
                "name": "flaky",
                "provider": "ollama",
                "base_url": "http://a",
                "price_per_mtok": {"input": 1.0, "output": 1.0},
            },
            {
                "name": "clean",
                "provider": "openai",
                "base_url": "http://b",
                "price_per_mtok": {"input": 1.0, "output": 1.0},
            },
        ],
    },
}


def _app(tmp_path, policy: dict | None = None, setups: dict | None = None):
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(setups or SETUPS))
    ppath = tmp_path / "policy.yaml"
    if policy is not None:
        ppath.write_text(yaml.safe_dump(policy))
    return server.create_app(
        models_path=models,
        client_factory=_Client,
        registry={},
        ignore_probes=True,
        policy_path=ppath,
    )


def _gen(client, **fields):
    return client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], **fields}
    )


def test_policy_narrows_and_refuses_with_403():
    policy = {"callers": {"replay": {"only": ["ollama"]}}}
    assert load_policy(yaml.safe_dump(policy)) == {"replay": ["ollama"]}
    with pytest.raises(SystemExit, match="non-empty"):
        load_policy(yaml.safe_dump({"callers": {"x": {}}}))


def test_a_governed_caller_reaches_only_what_policy_names(tmp_path):
    client = TestClient(_app(tmp_path, {"callers": {"replay": {"only": ["ollama"]}}}))
    ok = _gen(client, model="qwen", caller="replay")
    assert ok.status_code == 200 and ok.json()["model"] == "qwen@ollama"
    blocked = _gen(client, model="haiku", caller="replay")
    assert blocked.status_code == 403 and "policy" in blocked.json()["detail"]
    unlisted = _gen(client, model="haiku", caller="someone-else")
    assert unlisted.status_code == 200  # no entry = unrestricted, not forbidden


def test_policy_binds_pins_too(tmp_path):
    client = TestClient(_app(tmp_path, {"callers": {"replay": {"only": ["ollama"]}}}))
    out = _gen(client, pin="haiku", caller="replay")
    assert out.status_code == 403  # operator authority outranks the pin


def test_only_and_ignore_narrow_but_never_broaden(tmp_path):
    client = TestClient(_app(tmp_path))
    picked = _gen(client, model="qwen", only=["qwen@hosted"])
    assert picked.json()["model"] == "qwen@hosted"
    dropped = _gen(client, model="qwen", ignore=["openai"])
    assert dropped.json()["model"] == "qwen@ollama"
    # an `only` naming something outside the routed candidates adds nothing
    broaden = _gen(client, model="qwen", only=["haiku"])
    assert broaden.status_code == 400 and "narrowed" in broaden.json()["detail"]


def test_max_price_refuses_loudly_with_the_cheapest_named(tmp_path):
    client = TestClient(_app(tmp_path))
    served = _gen(client, model="qwen@hosted", max_price=5.0)
    assert served.status_code == 200
    refused = _gen(client, model="qwen@hosted", max_price=1.0)
    assert refused.status_code == 400
    assert "cheapest admitted endpoint costs 3.0" in refused.json()["detail"]
    pinned = _gen(client, pin="qwen@hosted", max_price=1.0)
    assert pinned.status_code == 400  # the ceiling binds pins too


def test_sort_disables_the_lottery_entirely(tmp_path):
    app = _app(tmp_path)
    gw = app.state.gateway

    class _Poisoned:
        def random(self):
            raise AssertionError("the lottery must not run under a sort override")

    gw.rng = _Poisoned()
    client = TestClient(app)
    out = _gen(client, model="qwen", sort="price")
    assert out.status_code == 200 and out.json()["model"] == "qwen@ollama"  # free is cheapest


def test_strict_latency_sort_puts_unmeasured_last(tmp_path):
    app = _app(tmp_path)
    gw = app.state.gateway
    gw.backend("qwen@hosted").ttft.observe(2.0)  # measured slow; ollama unmeasured
    client = TestClient(app)
    out = _gen(client, model="qwen", sort="latency")
    assert out.json()["model"] == "qwen@hosted"  # proven speed beats no evidence


def test_strict_throughput_sort_orders_descending(tmp_path):
    app = _app(tmp_path)
    gw = app.state.gateway
    gw.backend("qwen@ollama").tps.observe(10.0)
    gw.backend("qwen@hosted").tps.observe(50.0)
    out = _gen(TestClient(app), model="qwen", sort="throughput")
    assert out.json()["model"] == "qwen@hosted"


def test_a_soft_preference_deprioritizes_on_evidence_only(tmp_path):
    """Same price tier (preferences act after the free/priced boundary,
    which preempts everything — the first draft of this test learned
    that): flaky-but-fast vs clean-but-slow."""
    app = _app(tmp_path, setups=PRICED_PAIR)
    gw = app.state.gateway
    gw.backend("qwen@flaky").ttft.observe(0.2)
    for ok in (False, False, True, False):
        gw.backend("qwen@flaky").errors.observe(ok)
    gw.backend("qwen@clean").ttft.observe(5.0)
    client = TestClient(app)
    # preference first: each served request feeds the windows (the fake
    # client is instant), so the second call sees updated evidence
    preferred = _gen(client, model="qwen", preferred_max_latency=1.0)
    normal = _gen(client, model="qwen")
    # without the preference the clean-but-slow endpoint wins; stating it
    # flips the order — missing a stated preference ranks worse than
    # being soft-deprioritized
    assert preferred.json()["model"] == "qwen@flaky"
    assert normal.json()["model"] == "qwen@clean"


def test_an_unmeasured_endpoint_cannot_miss_a_preference(tmp_path):
    app = _app(tmp_path, setups=PRICED_PAIR)
    gw = app.state.gateway
    gw.backend("qwen@clean").ttft.observe(5.0)  # misses; flaky unmeasured
    out = _gen(TestClient(app), model="qwen", preferred_max_latency=1.0)
    assert out.json()["model"] == "qwen@flaky"  # nothing held against an empty window


def test_a_throughput_preference_deprioritizes_like_the_latency_one(tmp_path):
    app = _app(tmp_path, setups=PRICED_PAIR)
    gw = app.state.gateway
    gw.backend("qwen@flaky").tps.observe(50.0)
    for ok in (False, False, True, False):
        gw.backend("qwen@flaky").errors.observe(ok)
    gw.backend("qwen@clean").tps.observe(5.0)
    out = _gen(TestClient(app), model="qwen", preferred_min_throughput=10.0)
    assert out.json()["model"] == "qwen@flaky"  # clean misses the stated floor
