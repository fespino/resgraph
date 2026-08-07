"""The resgraph-evals CLI: report rendering and the run command's
wiring of flags into run_eval (#130). The heavy pieces — the API
client, the driver, the loop itself — are stubbed; what is under
test is that the CLI translates its surface faithfully."""

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from resgraph.evals.cli import app

runner = CliRunner()


def _row(scenario_id, scenario_type, dims):
    return {
        "scenario_id": scenario_id,
        "scenario_type": scenario_type,
        "trial": 0,
        "dims": {d: {"passed": p, "detail": ""} for d, p in dims.items()},
        "latency_s": 1.0,
        "cache_hit_rate": 0.95,
        "tokens": {"total": 1000},
        "cache_fingerprint": "abc",
        "degraded": False,
    }


def test_report_renders_a_run_file(tmp_path):
    f = tmp_path / "run.jsonl"
    rows = [
        _row("a", "direct", {"found_top3": True, "evidence": True}),
        _row("b", "control", {"honesty": True}),
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = runner.invoke(app, ["report", str(f), "--baseline", str(tmp_path / "none.json")])
    assert result.exit_code == 0
    assert "pass^k 1.00" in result.output
    assert "control" in result.output


def _stub_run(monkeypatch, tmp_path, captured):
    def fake_run_eval(scenarios, client, driver, **kwargs):
        captured.update(kwargs)
        captured["n_scenarios"] = len(scenarios)
        return tmp_path / "out.jsonl"

    monkeypatch.setattr("resgraph.evals.runner.run_eval", fake_run_eval)
    monkeypatch.setattr(
        "resgraph.graph.client.get_driver",
        lambda: SimpleNamespace(verify_connectivity=lambda: None),
    )
    monkeypatch.setattr("anthropic.Anthropic", object)


def test_run_wires_flags_into_run_eval(monkeypatch, tmp_path):
    captured = {}
    _stub_run(monkeypatch, tmp_path, captured)
    result = runner.invoke(
        app,
        ["run", "--no-judge", "--trials", "1", "--max-cost", "2.5", "--skip-preflight"],
    )
    assert result.exit_code == 0
    assert captured["judge_model"] is None
    assert captured["trials"] == 1
    assert captured["max_cost"] == 2.5
    assert captured["skip_preflight"] is True
    assert captured["n_scenarios"] == 30


def test_run_defaults_mean_no_cost_cap_and_preflight_on(monkeypatch, tmp_path):
    captured = {}
    _stub_run(monkeypatch, tmp_path, captured)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert captured["max_cost"] is None
    assert captured["skip_preflight"] is False
    assert captured["judge_model"] is not None


def _run_file(tmp_path, name, *, items=4, trials=3, passed=True):
    rows = [
        _row(f"s{i}", "direct", {"found_top3": passed, "evidence": True})
        for i in range(items)
        for _ in range(trials)
    ]
    f = tmp_path / name
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def _baseline_file(tmp_path, run_path):
    from resgraph.evals.report import aggregate

    rows = [json.loads(ln) for ln in run_path.read_text().splitlines() if ln.strip()]
    f = tmp_path / "baseline.json"
    f.write_text(json.dumps(aggregate(rows)))
    return f


def test_gate_exits_zero_when_the_run_matches_its_baseline(tmp_path):
    run_path = _run_file(tmp_path, "run.jsonl")
    base = _baseline_file(tmp_path, run_path)
    result = runner.invoke(app, ["gate", str(run_path), "--baseline", str(base)])
    assert result.exit_code == 0
    assert "EVAL GATE: PASS" in result.output


def test_gate_exits_one_on_a_regression(tmp_path):
    base = _baseline_file(tmp_path, _run_file(tmp_path, "good.jsonl"))
    regressed = _run_file(tmp_path, "bad.jsonl", passed=False)
    result = runner.invoke(app, ["gate", str(regressed), "--baseline", str(base)])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output


def test_gate_exits_three_when_it_cannot_verdict(tmp_path):
    base = _baseline_file(tmp_path, _run_file(tmp_path, "good.jsonl"))
    single = _run_file(tmp_path, "k1.jsonl", trials=1)
    result = runner.invoke(app, ["gate", str(single), "--baseline", str(base)])
    assert result.exit_code == 3
    assert "UNDECIDED" in result.output


def test_gate_exits_four_on_unreadable_evidence(tmp_path):
    base = _baseline_file(tmp_path, _run_file(tmp_path, "good.jsonl"))
    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json\n")
    result = runner.invoke(app, ["gate", str(broken), "--baseline", str(base)])
    assert result.exit_code == 4
    assert "ERROR" in result.output


def test_gate_rejects_a_missing_baseline_as_a_usage_error(tmp_path):
    run_path = _run_file(tmp_path, "run.jsonl")
    result = runner.invoke(app, ["gate", str(run_path), "--baseline", str(tmp_path / "no.json")])
    assert result.exit_code == 2
