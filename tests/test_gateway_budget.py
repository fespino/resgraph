"""The fall-forward spend budget: fallback-served paid traffic is capped,
free candidates and intended spend are not, and the refusal is distinct."""

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from resgraph.gateway.budget import FallForwardBudget
from resgraph.gateway.router import ClassRoute
from resgraph.gateway.server import create_app

SETUPS = {
    "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "qwen-local-1.5b": {
        "provider": "ollama",
        "model": "qwen2.5:1.5b",
        "base_url": "http://localhost:11434/v1",
    },
    # a self-hosted OpenAI-protocol server; the model name must not be a
    # real paid model — the phase audit caught this fixture calling gpt-4o
    # free, the exact convention slip the load-time warning now names
    "oss-free": {
        "provider": "openai",
        "model": "oss-local-8b",
        "base_url": "http://localhost:9999/v1",
    },
}

REGISTRY = {
    "judgment": ClassRoute("haiku", "paid"),
    "workhorse": ClassRoute("qwen-local-1.5b", "local"),
    "classification": ClassRoute("oss-free", "unpriced, hence free by convention"),
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
            usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
        )


@pytest.fixture
def harness(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    budget = FallForwardBudget(cap_usd=2.0, ledger=tmp_path / "fallback-spend.json")
    calls: list[tuple[str, dict]] = []
    behaviors: dict[str, str] = {}
    app = create_app(
        models_path=path,
        client_factory=lambda setup: FakeClient(setup["name"], behaviors, calls),
        registry=REGISTRY,
        fallback_budget=budget,
    )
    return TestClient(app), behaviors, calls, budget


def _gen(client: TestClient, **body: Any):
    return client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "hi"}], **body}
    )


def test_a_walk_to_a_paid_backend_within_budget_serves_and_charges(harness):
    client, behaviors, _, budget = harness
    behaviors["qwen-local-1.5b"] = "boom"
    behaviors["oss-free"] = "boom"
    r = _gen(client, task_class="workhorse")
    assert r.status_code == 200
    assert r.json()["backend"] == "anthropic"
    # 1M input tokens on haiku ($1/MTok) — the fake's usage prices to $1
    assert budget.spent_today() == pytest.approx(1.0)


def test_an_exhausted_budget_skips_paid_and_serves_a_free_candidate(harness):
    client, behaviors, calls, budget = harness
    budget.charge(2.0)
    behaviors["qwen-local-1.5b"] = "boom"
    r = _gen(client, task_class="workhorse")
    assert r.status_code == 200
    assert r.json()["backend"] == "openai"
    assert "haiku" not in [name for name, _ in calls]
    assert budget.spent_today() == pytest.approx(2.0)  # unpriced serve charges nothing


def test_an_exhausted_budget_with_only_paid_candidates_is_a_budget_503(harness):
    client, behaviors, calls, budget = harness
    budget.charge(2.0)
    behaviors["qwen-local-1.5b"] = "boom"
    behaviors["oss-free"] = "boom"
    r = _gen(client, task_class="workhorse")
    assert r.status_code == 503
    assert "budget" in r.json()["detail"]
    assert "haiku" not in [name for name, _ in calls]


def test_an_exhausted_walk_with_budget_headroom_stays_an_exhausted_503(harness):
    client, behaviors, _, _ = harness
    for alias in SETUPS:
        behaviors[alias] = "boom"
    r = _gen(client, task_class="workhorse")
    assert r.status_code == 503
    assert "budget" not in r.json()["detail"]


def test_a_pin_ignores_budget_state(harness):
    client, _, _, budget = harness
    budget.charge(2.0)
    r = _gen(client, pin="haiku")
    assert r.status_code == 200
    assert r.json()["backend"] == "anthropic"


def test_routed_paid_traffic_never_charges_the_fallback_ledger(harness):
    client, _, _, budget = harness
    r = _gen(client, task_class="judgment")
    assert r.status_code == 200
    assert budget.spent_today() == 0.0


def test_the_warn_line_logs_once(tmp_path, caplog):
    budget = FallForwardBudget(cap_usd=1.0, ledger=tmp_path / "spend.json")
    with caplog.at_level(logging.WARNING, logger="resgraph.gateway"):
        budget.charge(0.95)
        budget.charge(0.01)
    warns = [r for r in caplog.records if "fallback-budget" in r.message]
    assert len(warns) == 1


def test_the_ledger_resets_on_day_roll(tmp_path):
    ledger = tmp_path / "spend.json"
    ledger.write_text(json.dumps({"date": "2020-01-01", "spent_usd": 99.0, "warned": True}))
    budget = FallForwardBudget(cap_usd=2.0, ledger=ledger)
    assert budget.spent_today() == 0.0
    assert budget.allows()


def test_a_streamed_fall_forward_charges_the_budget(tmp_path):
    """The stream path charges too, so the anthropic adapter inherits the
    budget instead of reopening the hole."""

    def factory(alias: str, kwargs: Any):
        if alias != "haiku":
            raise ConnectionError("backend unreachable")
        return iter([("content", "ok"), ("usage", {"input_tokens": 1_000_000, "output_tokens": 0})])

    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(SETUPS))
    budget = FallForwardBudget(cap_usd=2.0, ledger=tmp_path / "spend.json")
    app = create_app(
        models_path=path,
        client_factory=lambda setup: None,
        registry=REGISTRY,
        stream_factory=factory,
        fallback_budget=budget,
    )
    r = _gen(TestClient(app), task_class="workhorse", stream=True)
    assert r.status_code == 200
    assert '"backend": "anthropic"' in r.text
    assert budget.spent_today() == pytest.approx(1.0)
