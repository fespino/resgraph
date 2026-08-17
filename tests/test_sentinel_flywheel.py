"""The queue and the flywheel: labels close loops in code, and the
gate blocks when a loop is broken."""

import json

import pytest
from typer.testing import CliRunner

from resgraph.sentinel import cli, queue, rules, scan


@pytest.fixture
def store(tmp_path):
    return queue.ReviewStore(path=tmp_path / "reviews.db")


def _decide(store, tmp_path, status):
    store.enqueue("r1", ["repeat_loop: an identical call repeated 30x"], "budget_abuse")
    return store.decide(
        "r1",
        status,
        "fran",
        exclusions_path=tmp_path / "excl.jsonl",
        confirmed_path=tmp_path / "conf.jsonl",
    )


def test_a_false_positive_writes_a_scoped_exclusion(store, tmp_path):
    d = _decide(store, tmp_path, "false_positive")
    assert d.exclusions_added == 1
    row = json.loads((tmp_path / "excl.jsonl").read_text())
    assert row == {"rule": "repeat_loop", "run_key": "r1"}


def test_a_confirmed_catch_joins_the_floor(store, tmp_path):
    d = _decide(store, tmp_path, "confirmed_malicious")
    assert d.confirmed_added == 1
    assert json.loads((tmp_path / "conf.jsonl").read_text())["run_key"] == "r1"


def test_decisions_are_append_only(store, tmp_path):
    _decide(store, tmp_path, "false_positive")
    with pytest.raises(SystemExit, match="already decided"):
        store.decide("r1", "confirmed_malicious", "fran")


def test_reenqueue_does_not_reopen(store, tmp_path):
    _decide(store, tmp_path, "false_positive")
    store.enqueue("r1", ["x"], None)
    assert store.pending() == []
    assert store.decided()[0]["status"] == "false_positive"


def test_time_to_decision_is_recorded(store, tmp_path):
    _decide(store, tmp_path, "unclear")
    assert store.decided()[0]["seconds_to_decision"] >= 0


def test_load_exclusions_round_trips_what_decide_writes(store, tmp_path):
    _decide(store, tmp_path, "false_positive")
    assert queue.load_exclusions(tmp_path / "excl.jsonl") == {("repeat_loop", "r1")}


def test_decide_refuses_bad_status_and_unknown_runs(store):
    with pytest.raises(SystemExit, match="status must be one of"):
        store.decide("r1", "pending", "fran")
    with pytest.raises(SystemExit, match="not in the queue"):
        store.decide("ghost", "unclear", "fran")


def test_a_scoped_exclusion_suppresses_one_rule_for_one_run_only():
    verdict = rules.scan_rules({"tool_trace": [], "tool_calls": 30, "tokens": {}})
    assert verdict.flagged
    excl = {("budget_anomaly", "runA")}
    assert not scan._excluded(verdict, "runA", excl).flagged
    assert scan._excluded(verdict, "runB", excl).flagged  # everywhere else: live


def test_the_gate_passes_on_the_committed_corpus():
    result = CliRunner().invoke(cli.app, ["gate"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_queue_cli_fill_list_decide_on_patched_paths(monkeypatch, tmp_path):
    """The operator wiring end to end: fill enqueues the funnel
    admissions, list shows the evidence, decide closes the loop —
    never touching the production db or the committed exclusions."""
    monkeypatch.setattr(queue, "DB_PATH", tmp_path / "reviews.db")
    monkeypatch.setattr(queue, "EXCLUSIONS_PATH", tmp_path / "excl.jsonl")
    monkeypatch.setattr(queue, "CONFIRMED_PATH", tmp_path / "conf.jsonl")
    runner = CliRunner()

    fill = runner.invoke(cli.app, ["queue", "fill"])
    assert fill.exit_code == 0, fill.output
    assert "29 runs in the queue" in fill.output

    listed = runner.invoke(cli.app, ["queue", "list"])
    assert listed.exit_code == 0
    assert "    - " in listed.output  # the evidence rides along
    key = listed.output.splitlines()[0].split()[0]

    args = ["queue", "decide", key, "confirmed_malicious", "--reviewer", "fran"]
    decided = runner.invoke(cli.app, args)
    assert decided.exit_code == 0, decided.output
    assert "confirmed+1" in decided.output
    assert json.loads((tmp_path / "conf.jsonl").read_text())["run_key"] == key
    assert runner.invoke(cli.app, args).exit_code != 0  # append-only via the CLI too


def test_the_gate_blocks_a_rule_that_flags_benign_traffic(monkeypatch):
    monkeypatch.setitem(rules.RULES, "noisy", lambda row, th: rules.Flag("noisy", "always"))
    result = CliRunner().invoke(cli.app, ["gate"])
    assert result.exit_code == 1
    assert "rule noisy flags benign traffic" in result.output
    assert "FP budget breached" in result.output


def test_the_gate_blocks_a_tuning_change_that_misses_a_known_attack(monkeypatch):
    def lobotomized(row, th):
        return None

    # probes are caught by exactly one rule (measured: their one planted
    # call is feature-invisible to L2), so killing it must block
    monkeypatch.setitem(rules.RULES, "forbidden_tool_attempt", lobotomized)
    result = CliRunner().invoke(cli.app, ["gate"])
    assert result.exit_code == 1
    assert "recall floor" in result.output
