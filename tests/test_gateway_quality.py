"""Eval-driven routing: the floor is a guarantee, the weights are
measured cost per passed triage, and every score carries its run."""

import random

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from resgraph.gateway import server
from resgraph.gateway.quality import eligible, load_quality
from resgraph.gateway.router import ClassRoute

TABLE = {
    "scores": {
        "judgment": {
            "good": {
                "passk": 0.9,
                "cost_per_passed": 0.111,
                "run": "evals/runs/a.jsonl",
                "date": "2026-08-19",
            },
            "cheapbad": {
                "passk": 0.05,
                "cost_per_passed": 0.02,
                "run": "evals/runs/b.jsonl",
                "date": "2026-08-19",
            },
        }
    }
}

SETUPS = {
    name: {"provider": "ollama", "base_url": f"http://{name}", "model": f"m-{name}"}
    for name in ("good", "cheapbad", "static")
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


REGISTRY = {
    "judgment": ClassRoute(
        "static", "test", candidates=("good", "cheapbad", "unmeasured"), min_passk=0.7
    )
}


def _app(tmp_path, table: dict | None = TABLE, registry=None):
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(SETUPS))
    qpath = tmp_path / "quality.yaml"
    if table is not None:
        qpath.write_text(yaml.safe_dump(table))
    return server.create_app(
        models_path=models,
        client_factory=_Client,
        registry=registry or REGISTRY,
        ignore_probes=True,
        quality_path=qpath,
    )


def _gen(client, **fields):
    return client.post(
        "/v1/generate", json={"messages": [{"role": "user", "content": "x"}], **fields}
    )


def test_the_loader_refuses_scores_without_provenance():
    with pytest.raises(SystemExit, match="opinion"):
        load_quality(yaml.safe_dump({"scores": {"judgment": {"a": {"passk": 0.9}}}}))
    with pytest.raises(SystemExit, match="not in"):
        load_quality(
            yaml.safe_dump({"scores": {"judgment": {"a": {"passk": 1.9, "run": "r", "date": "d"}}}})
        )


def test_the_floor_excludes_and_unmeasured_is_ineligible():
    table = load_quality(yaml.safe_dump(TABLE))
    assert eligible(table, "judgment", ["good", "cheapbad", "unmeasured"], 0.7) == ["good"]
    assert eligible(table, "judgment", ["unmeasured"], 0.0) == []  # no eval, no route


def test_quality_routing_picks_the_measured_arm_over_the_cheapest(tmp_path):
    out = _gen(TestClient(_app(tmp_path)), task_class="judgment")
    assert out.status_code == 200
    body = out.json()
    # cheapbad is 5x cheaper per passed triage and still loses: the floor
    # comes before the weights, so cheap never buys wrong
    assert body["model"] == "good"
    assert body["source"] == "quality_route"


def test_no_eligible_candidate_degrades_to_the_static_default(tmp_path):
    strict = {"judgment": ClassRoute("static", "test", candidates=("cheapbad",), min_passk=0.7)}
    out = _gen(TestClient(_app(tmp_path, registry=strict)), task_class="judgment")
    assert out.status_code == 200
    assert out.json()["model"] == "static"
    assert out.json()["source"] == "task_class_default"  # degraded, never refused


def test_a_missing_table_leaves_routing_exactly_as_before(tmp_path):
    out = _gen(TestClient(_app(tmp_path, table=None)), task_class="judgment")
    assert out.json()["model"] == "static"
    assert out.json()["source"] == "task_class_default"


def test_a_free_arm_above_the_floor_preempts_the_lottery(tmp_path):
    table = {
        "scores": {
            "judgment": {
                "good": TABLE["scores"]["judgment"]["good"],
                "cheapbad": {
                    "passk": 0.8,
                    "cost_per_passed": 0,
                    "run": "r",
                    "date": "d",
                },
            }
        }
    }
    out = _gen(TestClient(_app(tmp_path, table=table)), task_class="judgment")
    assert out.json()["model"] == "cheapbad"  # free and above the floor: no lottery


def test_a_route_without_candidates_never_enters_quality_routing(tmp_path):
    registry = {**REGISTRY, "workhorse": ClassRoute("static", "test")}
    out = _gen(TestClient(_app(tmp_path, registry=registry)), task_class="workhorse")
    assert out.json()["source"] == "task_class_default"


def test_the_builder_refuses_a_malformed_spec(tmp_path):
    from resgraph.evals import cli as evals_cli

    r = CliRunner().invoke(evals_cli.app, ["routing-table", "no-equals-here"])
    assert r.exit_code != 0


def test_a_bare_request_takes_the_global_default_untouched(tmp_path):
    out = _gen(TestClient(_app(tmp_path)))  # no pin, no model, no task_class
    assert out.status_code == 400  # global default alias is not in this catalog
    assert "qwen-local-1.5b" in out.json()["detail"]


def test_pins_and_overrides_outrank_quality_routing(tmp_path):
    client = TestClient(_app(tmp_path))
    assert _gen(client, task_class="judgment", model="static").json()["source"] == "override"
    assert _gen(client, task_class="judgment", pin="static").json()["source"] == "pin"


def test_the_builder_derives_the_table_from_a_committed_run(tmp_path):
    from resgraph.evals import cli as evals_cli

    out = tmp_path / "quality.yaml"
    run = "evals/runs/20260803T121152Z.jsonl"
    r = CliRunner().invoke(
        evals_cli.app,
        ["routing-table", f"opus={run}", "--task-class", "judgment", "--out", str(out)],
    )
    assert r.exit_code == 0, r.output
    table = load_quality(out.read_text())
    entry = table["judgment"]["opus"]
    assert 0.0 <= entry["passk"] <= 1.0
    assert entry["run"] == run and entry["date"] == "2026-08-03"  # provenance from the run itself


def test_the_replay_comparison_price_only_vs_quality_floored(tmp_path):
    """The exit-gate measurement: the same request stream under both
    policies, on fixture arms mirroring the measured shapes (a strong
    arm at real cost vs a cheap arm that rarely passes). Deterministic;
    the assertions are the receipt."""
    app = _app(tmp_path)
    gw = app.state.gateway
    gw.rng = random.Random(7)
    client = TestClient(app)
    n = 200
    picks = [(_gen(client, task_class="judgment")).json()["model"] for _ in range(n)]
    scores = TABLE["scores"]["judgment"]
    quality_solved = sum(scores[p]["passk"] for p in picks)
    quality_spend = sum(scores[p]["passk"] * scores[p]["cost_per_passed"] for p in picks)
    # price-only picks the cheapest cost-per-passed arm every time
    price_pick = min(scores, key=lambda a: scores[a]["cost_per_passed"])
    price_solved = n * scores[price_pick]["passk"]
    assert picks and set(picks) == {"good"}
    assert quality_solved == pytest.approx(180.0)  # 0.9 * 200
    assert price_solved == pytest.approx(10.0)  # 0.05 * 200: cheapest solves 5%
    assert quality_solved / price_solved == 18.0  # the floor is the whole difference
    assert quality_spend == pytest.approx(180 * 0.111)
