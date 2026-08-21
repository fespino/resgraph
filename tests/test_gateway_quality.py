"""Eval-driven routing: the floor is a guarantee, the weights are
measured cost per passed triage, and every score carries its run."""

import random

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from resgraph.gateway import server
from resgraph.gateway.quality import (
    AXES,
    dominates,
    eligible,
    fabricating,
    frontier,
    load_quality,
    stale,
)
from resgraph.gateway.router import ClassRoute

TABLE = {
    "scores": {
        "judgment": {
            "good": {
                "passk": 0.9,
                "cost_per_passed": 0.111,
                "fabrication_count": 0,
                "run": "evals/runs/a.jsonl",
                "date": "2026-08-19",
            },
            "cheapbad": {
                "passk": 0.05,
                "cost_per_passed": 0.02,
                "fabrication_count": 0,
                "run": "evals/runs/b.jsonl",
                "date": "2026-08-19",
            },
        }
    }
}

SETUPS = {
    name: {"provider": "ollama", "base_url": f"http://{name}", "model": f"m-{name}"}
    for name in ("good", "cheapbad", "honest", "static")
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


def _entry(*, passk: float, **fields):
    """The provenance the loader requires, around a score the test
    states itself. passk has no default: a table fixture that hid the
    number the assertion turns on would document nothing."""
    return {"passk": passk, "fabrication_count": 0, "run": "r", "date": "2026-08-19"} | fields


def test_the_loader_refuses_scores_without_provenance():
    with pytest.raises(SystemExit, match="opinion"):
        load_quality(yaml.safe_dump({"scores": {"judgment": {"a": {"passk": 0.9}}}}))
    with pytest.raises(SystemExit, match="not in"):
        load_quality(yaml.safe_dump({"scores": {"judgment": {"a": _entry(passk=1.9)}}}))


def test_a_score_that_never_counted_fabrications_is_not_a_clean_score():
    """Absence is not zero here: an entry generated before the count
    was carried would otherwise route as if the dimension had passed."""
    silent = {k: v for k, v in _entry(passk=0.9).items() if k != "fabrication_count"}
    with pytest.raises(SystemExit, match="fabrication_count"):
        load_quality(yaml.safe_dump({"scores": {"judgment": {"a": silent}}}))


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
                "cheapbad": _entry(passk=0.8, cost_per_passed=0),
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


def test_dominance_needs_every_axis_the_arms_recorded():
    """Two axes would prune an arm that trades quality for speed —
    which is why the table stopped discarding latency."""
    good = {"passk": 0.9, "cost_per_passed": 0.10, "latency_p50_s": 4.0}
    worse = {"passk": 0.7, "cost_per_passed": 0.15, "latency_p50_s": 9.0}
    faster = {"passk": 0.7, "cost_per_passed": 0.15, "latency_p50_s": 0.5}
    assert dominates(good, worse)
    assert not dominates(good, faster)
    assert not dominates(worse, good)
    scores = {"good": good, "worse": worse, "faster": faster}
    assert frontier(scores, ["good", "worse", "faster"]) == ["good", "faster"]


def test_dominance_ignores_axes_one_side_never_recorded():
    old = {"passk": 0.9, "cost_per_passed": 0.10}  # a table written before latency was kept
    new = {"passk": 0.8, "cost_per_passed": 0.20, "latency_p50_s": 1.0}
    assert dominates(old, new)  # compared on what both carry, not on absence
    assert frontier({"a": old, "b": new}, ["a", "b"]) == ["a"]
    assert not dominates({"passk": 0.5}, {"cost_per_passed": 0.1})  # nothing in common


def test_staleness_is_measured_in_days_and_never_routes():
    scores = {
        "fresh": {"passk": 0.9, "cost_per_passed": 0.1, "date": "2026-08-01"},
        "old": {"passk": 0.9, "cost_per_passed": 0.1, "date": "2026-01-01"},
        "unparseable": {"passk": 0.9, "cost_per_passed": 0.1, "date": "last tuesday"},
    }
    aged = stale(scores, list(scores), "2026-08-21", 90)
    assert aged == ["old"]  # the unparseable date is not silently treated as ancient
    assert stale(scores, ["missing"], "2026-08-21", 90) == []


def test_the_router_refuses_to_spend_on_a_dominated_arm(tmp_path, caplog):
    """The receipt: an arm that clears the floor but loses on every
    axis drew a third of the stream under the lottery alone, and
    serving it could not even update its own score."""
    table = {
        "scores": {
            "judgment": {
                "good": _entry(passk=0.9, cost_per_passed=0.10, latency_p50_s=4.0),
                "cheapbad": _entry(passk=0.75, cost_per_passed=0.15, latency_p50_s=9.0),
            }
        }
    }
    registry = {
        "judgment": ClassRoute("static", "test", candidates=("good", "cheapbad"), min_passk=0.7)
    }
    app = _app(tmp_path, table=table, registry=registry)
    gw = app.state.gateway
    gw.rng = random.Random(7)
    client = TestClient(app)
    with caplog.at_level("INFO", logger="resgraph.gateway"):
        picks = [_gen(client, task_class="judgment").json()["model"] for _ in range(200)]
    assert set(picks) == {"good"}  # both cleared the floor; only one is on the frontier
    assert any("worse on every measured axis" in r.getMessage() for r in caplog.records)

    scores = table["scores"]["judgment"]
    weights = {a: 1.0 / scores[a]["cost_per_passed"] ** 2 for a in scores}
    share = weights["cheapbad"] / sum(weights.values())
    assert share == pytest.approx(0.308, abs=0.005)  # what price-weighting alone would spend
    solved_before = 200 * (
        (1 - share) * scores["good"]["passk"] + share * scores["cheapbad"]["passk"]
    )
    assert solved_before == pytest.approx(170.8, abs=0.5)
    assert 200 * scores["good"]["passk"] == 180.0  # excluding it buys ~9 solved runs per 200


def test_an_arm_that_fabricated_is_disqualified_not_merely_ranked(tmp_path, caplog):
    """The eval gate blocks a merge on a fabrication unconditionally,
    so the router cannot treat one as a weaker arm and buy it anyway —
    even when it is the only candidate above the floor."""
    table = {
        "scores": {
            "judgment": {
                "good": _entry(passk=0.95, cost_per_passed=0.01, fabrication_count=1),
                "honest": _entry(passk=0.8, cost_per_passed=0.50),
            }
        }
    }
    registry = {
        "judgment": ClassRoute("static", "test", candidates=("good", "honest"), min_passk=0.7)
    }
    loaded = load_quality(yaml.safe_dump(table))
    assert fabricating(loaded, "judgment", ["good", "honest"]) == ["good"]
    assert eligible(loaded, "judgment", ["good", "honest"], 0.7) == ["honest"]
    app = _app(tmp_path, table=table, registry=registry)
    with caplog.at_level("WARNING", logger="resgraph.gateway"):
        out = _gen(TestClient(app), task_class="judgment")
    assert out.json()["model"] == "honest"  # cheaper and higher pass^k, and still not served
    assert any("the measured run fabricated" in r.getMessage() for r in caplog.records)


def test_the_tail_is_an_axis_because_the_median_hides_a_bimodal_backend():
    """p50 alone admitted an arm whose deadline behaviour is worse:
    TTFT on the local backend is bimodal (docs/capacity.md), so the
    median and the tail can disagree about which arm a caller wants."""
    steady = {"passk": 0.9, "cost_per_passed": 0.1, "latency_p50_s": 2.0, "latency_p95_s": 2.4}
    spiky = {"passk": 0.9, "cost_per_passed": 0.1, "latency_p50_s": 1.5, "latency_p95_s": 30.0}
    assert not dominates(steady, spiky) and not dominates(spiky, steady)
    assert frontier({"steady": steady, "spiky": spiky}, ["steady", "spiky"]) == ["steady", "spiky"]
    # on p50 alone the spiky arm dominated outright, and the tail never argued
    p50_only = [(k, low) for k, low in AXES if k != "latency_p95_s"]
    assert dominates({k: spiky[k] for k, _ in p50_only}, {k: steady[k] for k, _ in p50_only})


def test_the_builder_carries_every_declared_input_from_a_real_run(tmp_path):
    from resgraph.evals import cli as evals_cli

    out = tmp_path / "quality.yaml"
    r = CliRunner().invoke(
        evals_cli.app,
        [
            "routing-table",
            "haiku=evals/runs/20260803T121152Z.jsonl",
            "--task-class",
            "judgment",
            "--out",
            str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    entry = load_quality(out.read_text())["judgment"]["haiku"]
    assert entry["fabrication_count"] == 0  # the run's own count, not an assumption
    assert entry["workers"], "the score names the worker that earned it"
    assert entry["latency_p95_s"] is not None and entry["latency_p95_s"] >= entry["latency_p50_s"]


def test_old_evidence_asks_to_be_re_measured_not_re_routed(tmp_path, caplog):
    """Staleness is announced once at load, as a request to re-run the
    arms — never as a share of traffic, because a served request
    produces no pass^k to refresh the score with."""
    table = {
        "scores": {"judgment": {"good": _entry(passk=0.9, cost_per_passed=0.10, date="2020-01-01")}}
    }
    with caplog.at_level("WARNING", logger="resgraph.gateway"):
        _app(tmp_path, table=table)
    aged = [r.getMessage() for r in caplog.records if "re-run the arms" in r.getMessage()]
    assert len(aged) == 1
    assert "'good'" in aged[0]
