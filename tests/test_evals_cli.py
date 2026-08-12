"""The resgraph-evals CLI: report rendering and the run command's
wiring of flags into run_eval (#130). The heavy pieces — the API
client, the driver, the loop itself — are stubbed; what is under
test is that the CLI translates its surface faithfully."""

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from resgraph.evals.cli import app

runner = CliRunner()


def _row(scenario_id, scenario_type, dims, model=None):
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
        "model": model,
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


def test_run_resolves_worker_and_judge_from_setups(monkeypatch, tmp_path):
    cfg = tmp_path / "w.yaml"
    cfg.write_text(
        "qwen:\n  provider: ollama\n  model: qwen2.5:7b\n  base_url: http://x/v1\n"
        "opus:\n  provider: anthropic\n  model: claude-opus-4-8\n"
    )
    captured = {}
    _stub_run(monkeypatch, tmp_path, captured)
    result = runner.invoke(
        app,
        [
            "run",
            "--worker",
            "qwen",
            "--judge",
            "opus",
            "--workers-config",
            str(cfg),
            "--trials",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["model"] == "qwen2.5:7b"
    assert captured["judge_model"] == "claude-opus-4-8"
    # the resolved setup objects ride the run, self-contained (like git_ref)
    assert captured["provenance"]["worker"]["provider"] == "ollama"
    assert captured["provenance"]["worker"]["base_url"] == "http://x/v1"
    assert captured["provenance"]["judge"]["provider"] == "anthropic"


def test_run_defaults_mean_no_cost_cap_and_preflight_on(monkeypatch, tmp_path):
    captured = {}
    _stub_run(monkeypatch, tmp_path, captured)
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert captured["max_cost"] is None
    assert captured["skip_preflight"] is False
    assert captured["judge_model"] is not None


def _run_file(tmp_path, name, *, items=4, trials=3, passed=True, model=None):
    rows = [
        _row(f"s{i}", "direct", {"found_top3": passed, "evidence": True}, model=model)
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


def test_gate_exits_four_on_an_unreadable_baseline(tmp_path):
    run_path = _run_file(tmp_path, "run.jsonl")
    broken = tmp_path / "baseline.json"
    broken.write_text("{not json\n")
    result = runner.invoke(app, ["gate", str(run_path), "--baseline", str(broken)])
    assert result.exit_code == 4
    assert "ERROR" in result.output


def test_gate_rejects_a_missing_baseline_as_a_usage_error(tmp_path):
    run_path = _run_file(tmp_path, "run.jsonl")
    result = runner.invoke(app, ["gate", str(run_path), "--baseline", str(tmp_path / "no.json")])
    assert result.exit_code == 2


def test_gate_directory_skips_a_run_of_a_different_worker(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    opus = _run_file(runs, "20260101T000000Z.jsonl", model="claude-opus-4-8")
    _run_file(runs, "20260201T000000Z.jsonl", model="qwen2.5:7b", passed=False)  # newer local arm
    base = _baseline_file(tmp_path, opus)  # baseline carries model=claude-opus-4-8
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(base)])
    assert result.exit_code == 0
    assert "worker qwen2.5:7b != baseline claude-opus-4-8" in result.output
    assert "gating `20260101T000000Z.jsonl`" in result.output


def _arm_file(tmp_path, name, model, passes):
    rows = [
        {
            "scenario_id": f"s{i}",
            "scenario_type": "direct",
            "tags": ["direct"],
            "trial": 0,
            "dims": {
                "found_top3": {"passed": p, "detail": ""},
                "evidence": {"passed": True, "detail": ""},
            },
            "tokens": {
                "input": 0,
                "output": 100_000,
                "cache_read": 0,
                "cache_creation": 0,
                "total": 100_000,
            },
            "latency_s": 12.0,
            "model": model,
        }
        for i, p in enumerate(passes)
    ]
    f = tmp_path / name
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def test_arms_renders_the_tier_table(tmp_path):
    opus = _arm_file(tmp_path, "opus.jsonl", "claude-opus-4-8", [True, True, False])
    sonnet = _arm_file(tmp_path, "sonnet.jsonl", "claude-sonnet-4-6", [True, False, False])
    result = runner.invoke(app, ["arms", f"opus={opus}", f"sonnet={sonnet}", "--baseline", "opus"])
    assert result.exit_code == 0
    assert "$/passed" in result.output and "sonnet" in result.output
    assert "p95s" in result.output and "12.0" in result.output  # latency rides alongside cost


def test_arms_declines_on_mismatched_item_sets(tmp_path):
    opus = _arm_file(tmp_path, "opus.jsonl", "claude-opus-4-8", [True, True])
    sonnet = _arm_file(tmp_path, "sonnet.jsonl", "claude-sonnet-4-6", [True])
    result = runner.invoke(app, ["arms", f"opus={opus}", f"sonnet={sonnet}", "--baseline", "opus"])
    assert result.exit_code == 3
    assert "DECLINED" in result.output


def test_arms_rejects_a_spec_without_a_label(tmp_path):
    opus = _arm_file(tmp_path, "opus.jsonl", "claude-opus-4-8", [True])
    result = runner.invoke(app, ["arms", str(opus)])
    assert result.exit_code == 2


def _skill_arm_file(tmp_path, name, passes_and_tools):
    rows = [
        {
            "scenario_id": f"s{i}",
            "scenario_type": "direct",
            "tags": ["direct"],
            "trial": 0,
            "dims": {
                "found_top3": {"passed": p, "detail": ""},
                "evidence": {"passed": True, "detail": ""},
            },
            "tool_trace": [{"tool": t, "ok": True} for t in tools],
        }
        for i, (p, tools) in enumerate(passes_and_tools)
    ]
    f = tmp_path / name
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def test_skill_value_renders_the_ledger(tmp_path):
    withf = _skill_arm_file(tmp_path, "with.jsonl", [(True, ["world_diff", "blast_radius"])])
    without = _skill_arm_file(tmp_path, "without.jsonl", [(False, ["resource_history"])])
    result = runner.invoke(app, ["skill-value", str(withf), str(without)])
    assert result.exit_code == 0
    assert "relevant" in result.output and "invoked" in result.output


def test_skill_value_declines_on_mismatched_item_sets(tmp_path):
    withf = _skill_arm_file(tmp_path, "with.jsonl", [(True, ["world_diff", "blast_radius"])])
    without = tmp_path / "without.jsonl"
    without.write_text(
        json.dumps(
            {
                "scenario_id": "other",
                "scenario_type": "direct",
                "tags": ["direct"],
                "trial": 0,
                "dims": {
                    "found_top3": {"passed": True, "detail": ""},
                    "evidence": {"passed": True, "detail": ""},
                },
                "tool_trace": [],
            }
        )
        + "\n"
    )
    result = runner.invoke(app, ["skill-value", str(withf), str(without)])
    assert result.exit_code == 3


def _companion_run(tmp_path, name, tag="store_degraded"):
    rows = [_row(f"c{i}", "direct", {"degraded": True}) for i in range(3)]
    for r in rows:
        r["tags"] = [tag]
    f = tmp_path / name
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def _base_run(tmp_path, name, prefix="s", trials=3):
    rows = [
        {
            **_row(f"{prefix}{i}", "direct", {"found_top3": True, "evidence": True}),
            "tags": ["direct"],
        }
        for i in range(4)
        for _ in range(trials)
    ]
    f = tmp_path / name
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def test_gate_directory_skips_companion_runs_and_gates_the_base(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    base = _base_run(runs, "20260101T000000Z.jsonl")
    _companion_run(runs, "20260102T000000Z.jsonl")  # newer, must be skipped
    baseline = _baseline_file(tmp_path, base)
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(baseline)])
    assert result.exit_code == 0
    assert "skipped 1 non-gateable run" in result.output and "companion-set" in result.output
    assert "gating `20260101T000000Z.jsonl`" in result.output


def test_gate_directory_declines_a_drifted_base_run_it_must_not_skip(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    baseline = _baseline_file(tmp_path, _base_run(tmp_path, "seed.jsonl", prefix="s"))
    _base_run(
        runs, "20260103T000000Z.jsonl", prefix="drift"
    )  # different item set, no companion tags
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(baseline)])
    assert result.exit_code == 3  # declined, not skipped
    assert "UNDECIDED" in result.output


def test_gate_directory_with_only_companion_runs_skips(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    _companion_run(runs, "20260101T000000Z.jsonl")
    baseline = _baseline_file(tmp_path, _base_run(tmp_path, "seed.jsonl"))
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(baseline)])
    assert result.exit_code == 0
    assert "No gateable run" in result.output


def test_gate_directory_skips_a_sub_k_base_run_to_find_a_verdictable_one(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    base = _base_run(runs, "20260101T000000Z.jsonl", trials=3)
    _base_run(runs, "20260102T000000Z.jsonl", trials=1)  # newer but k=1, cannot verdict
    baseline = _baseline_file(tmp_path, base)
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(baseline)])
    assert result.exit_code == 0
    assert "k=1<3" in result.output and "gating `20260101T000000Z.jsonl`" in result.output


def test_gate_directory_md_skip_is_titled(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    _companion_run(runs, "20260101T000000Z.jsonl")
    baseline = _baseline_file(tmp_path, _base_run(tmp_path, "seed.jsonl"))
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(baseline), "--md"])
    assert result.exit_code == 0
    assert result.output.startswith("## ⏭️ Eval gate — skipped")


def test_gate_directory_skips_an_unreadable_run_file(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    base = _base_run(runs, "20260101T000000Z.jsonl")
    (runs / "20260102T000000Z.jsonl").write_text("{not json\n")  # newest, unreadable
    baseline = _baseline_file(tmp_path, base)
    result = runner.invoke(app, ["gate", str(runs), "--baseline", str(baseline)])
    assert result.exit_code == 0
    assert "gating `20260101T000000Z.jsonl`" in result.output
