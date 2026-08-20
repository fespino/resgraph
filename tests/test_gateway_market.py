"""The market connector: polite ingestion, drift refusal at both
boundaries, prose redaction, and the one consumer — ours vs market."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from resgraph.gateway import market

ROW = {
    "id": "anthropic/claude-haiku-4.5",
    "name": "Anthropic: Claude Haiku 4.5",
    "context_length": 200000,
    "description": "Authored prose about the model.",
    "pricing": {"prompt": "0.000001", "completion": "0.000005"},
}


def _transport(handler):
    return httpx.MockTransport(handler)


def test_fetch_validates_the_happy_path():
    rows = market.fetch(transport=_transport(lambda req: httpx.Response(200, json={"data": [ROW]})))
    assert rows[0]["id"] == "anthropic/claude-haiku-4.5"


def test_a_401_is_a_defined_outcome_not_an_error():
    """The open access is observed behavior, not contract: when the
    door closes the connector says so and stops — no retry loop."""
    with pytest.raises(SystemExit, match="stop polling"):
        market.fetch(transport=_transport(lambda req: httpx.Response(401)))
    with pytest.raises(SystemExit, match="stop for this run"):
        market.fetch(transport=_transport(lambda req: httpx.Response(429)))
    with pytest.raises(SystemExit, match="HTTP 500"):
        market.fetch(transport=_transport(lambda req: httpx.Response(500)))


def test_the_pull_identifies_itself():
    """The pre-flight condition: attributable traffic, a User-Agent
    naming this repo on every request."""
    seen = {}

    def handler(req):
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(200, json={"data": [ROW]})

    market.fetch(transport=_transport(handler))
    assert "github.com/fespino/resgraph" in seen["ua"]


def test_shape_drift_refuses_and_names_the_problem():
    with pytest.raises(SystemExit, match="non-empty 'data' list"):
        market.validate({"models": []})
    with pytest.raises(SystemExit, match="lacks \\['pricing'\\]"):
        market.validate({"data": [{k: v for k, v in ROW.items() if k != "pricing"}]})
    with pytest.raises(SystemExit, match="float-parseable"):
        market.validate({"data": [{**ROW, "pricing": {"prompt": "call us", "completion": "1"}}]})


def test_redaction_keeps_the_shape_and_drops_the_prose():
    (out,) = market.redact([ROW])
    assert set(out) == set(ROW)  # every key survives: drift tests want the whole shape
    assert out["description"] == market.REDACTED
    assert out["pricing"] == ROW["pricing"]


def test_snapshot_round_trip_carries_provenance(tmp_path):
    path = market.snapshot(
        [ROW],
        path=tmp_path / "openrouter-2026-08-20.json",
        url=market.MODELS_URL,
        fetched_at="2026-08-20T00:38:32+00:00",
    )
    doc = market.load_snapshot(path)
    assert doc["source"] == market.MODELS_URL
    assert doc["model_count"] == 1
    assert doc["data"][0]["description"] == market.REDACTED


def test_a_snapshot_without_provenance_is_refused(tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text(json.dumps({"data": [ROW]}))
    with pytest.raises(SystemExit, match="no provenance, no baseline"):
        market.load_snapshot(bad)
    drifted = tmp_path / "d.json"
    drifted.write_text(json.dumps({"source": "x", "fetched_at": "y", "data": "not a list"}))
    with pytest.raises(SystemExit, match="drifted"):
        market.load_snapshot(drifted)


def test_matching_is_mechanical_and_ambiguity_matches_nothing():
    twin = {**ROW, "id": "mirror/claude-haiku-4.5"}
    prices = market.market_prices([ROW, twin])
    assert "claude-haiku-4-5" not in prices  # two authors, one tail: no auto-match
    (only,) = market.market_prices([ROW]).values()
    assert only["id"] == "anthropic/claude-haiku-4.5"
    assert only["per_mtok"] == pytest.approx(6.0)


def test_the_baseline_is_the_run_local_vs_route_table():
    rows = [
        ROW,
        {
            "id": "qwen/qwen-2.5-7b-instruct",
            "name": "Qwen 2.5 7B",
            "context_length": 32768,
            "pricing": {"prompt": "0.00000004", "completion": "0.0000001"},
        },
    ]
    table = {
        "haiku": {"model": "claude-haiku-4-5"},
        "qwen-local": {"model": "qwen2.5:7b", "market": "qwen/qwen-2.5-7b-instruct"},
        "mystery": {"model": "nobody-lists-this"},
    }
    out = {r["endpoint"]: r for r in market.baseline(table, {"claude-haiku-4-5": (1.0, 5.0)}, rows)}
    assert out["haiku"]["ratio"] == pytest.approx(1.0)  # same weights, same price
    assert out["qwen-local"]["ours_per_mtok"] is None  # local serving is the free tier
    assert out["qwen-local"]["market_per_mtok"] == pytest.approx(0.14)
    assert out["mystery"]["market_id"] is None  # unmatched is a fact, not a zero


def test_the_committed_snapshot_still_validates():
    """The drift gate applied to the real artifact: the committed
    snapshot loads, validates, and carries no authored prose."""
    path = max(market.SNAPSHOT_DIR.glob("openrouter-*.json"))
    doc = market.load_snapshot(path)
    assert doc["model_count"] == len(doc["data"])
    assert all(r.get("description") in (None, "", market.REDACTED) for r in doc["data"])


def test_the_cli_pulls_redacts_and_reports(tmp_path, monkeypatch):
    from resgraph.gateway import cli

    monkeypatch.setattr(market, "fetch", lambda url: [ROW])
    r = CliRunner().invoke(cli.app, ["market-pull", "--out-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    (path,) = tmp_path.glob("openrouter-*.json")
    assert market.REDACTED in path.read_text()
    assert "1 models" in r.output


def test_the_cli_baseline_reads_the_newest_snapshot(tmp_path):
    import yaml

    from resgraph.gateway import cli

    market.snapshot(
        [ROW],
        path=tmp_path / "openrouter-2026-08-20.json",
        url=market.MODELS_URL,
        fetched_at="2026-08-20T00:38:32+00:00",
    )
    models = tmp_path / "models.yaml"
    models.write_text(
        yaml.safe_dump(
            {
                "haiku": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                "local": {"provider": "ollama", "model": "nobody-lists-this"},
            }
        )
    )
    r = CliRunner().invoke(
        cli.app,
        [
            "market-baseline",
            "--snapshot",
            str(tmp_path / "openrouter-2026-08-20.json"),
            "--models-config",
            str(models),
        ],
    )
    assert r.exit_code == 0, r.output
    assert "haiku: ours=$6.00 market=$6.00 (anthropic/claude-haiku-4.5) ratio=1.0" in r.output
    assert "local: ours=free market=unmatched" in r.output


def test_the_cli_baseline_without_a_snapshot_says_pull_first(tmp_path, monkeypatch):
    from resgraph.gateway import cli

    monkeypatch.setattr(market, "SNAPSHOT_DIR", tmp_path)
    r = CliRunner().invoke(cli.app, ["market-baseline"])
    assert r.exit_code == 1
    assert "run market-pull first" in str(r.exception)


def test_row_shapes_are_a_fingerprint_across_pulls_not_a_count_within_one():
    """The catalog's own rows differ by design — an omitted optional
    field is not drift — so shape is only a question about pulls."""
    thin = {k: v for k, v in ROW.items() if k != "description"}
    assert len(market.field_sets([ROW, thin])) == 2  # normal, not a signal
    assert market.drift([ROW, thin], [ROW, thin]) == []


def test_drift_names_fields_nobody_declared():
    before = [ROW]
    after = [{**ROW, "reasoning": {"enabled": True}}, {**ROW, "id": "x/y"}]
    findings = market.drift(before, after)
    assert "fields new since the previous pull: ['reasoning']" in findings
    assert "distinct row shapes: 1 -> 2" in findings
    gone = market.drift([{**ROW, "retired_field": 1}], [ROW])
    assert "fields gone since the previous pull: ['retired_field']" in gone


def test_the_committed_snapshot_has_no_drift_against_itself():
    doc = market.load_snapshot(max(market.SNAPSHOT_DIR.glob("openrouter-*.json")))
    assert market.drift(doc["data"], doc["data"]) == []
    assert len(market.field_sets(doc["data"])) == 5  # the live catalog's real spread
