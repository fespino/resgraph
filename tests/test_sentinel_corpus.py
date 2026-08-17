"""The detection corpus: deterministic, ground-truthed, sanitized."""

import json

import pytest
from typer.testing import CliRunner

from resgraph.sentinel import cli, corpus


@pytest.fixture
def benign():
    return corpus.iter_benign()


@pytest.fixture
def attacks():
    return corpus.load_attacks()


def test_the_committed_attacks_rebuild_byte_for_byte(benign, tmp_path):
    rebuilt = tmp_path / "attacks.jsonl"
    corpus.write_attacks(corpus.build_attacks(benign), rebuilt)
    assert rebuilt.read_bytes() == corpus.ATTACKS_PATH.read_bytes()


def test_every_attack_type_is_present(attacks):
    types = {a["sentinel"]["attack_type"] for a in attacks}
    assert types == set(corpus.ATTACK_TYPES)


def test_every_attack_carries_ground_truth(attacks):
    for a in attacks:
        assert a["sentinel"]["malicious"] is True
        assert a["sentinel"]["attack_type"] in corpus.ATTACK_TYPES
        assert a["sentinel"]["base_run"]


def test_the_benign_corpus_carries_no_malice_label(benign):
    assert benign
    assert not any("sentinel" in row for row in benign)


def test_only_injection_items_carry_the_seed_marker(attacks):
    for a in attacks:
        blob = json.dumps(a)
        if a["sentinel"]["attack_type"] == "injection":
            assert corpus.INJECTION_TEMPLATE[:14] in blob
        else:
            assert "[SENTINEL-SEED]" not in blob


def test_the_benign_corpus_never_carries_the_seed_marker(benign):
    assert not any("[SENTINEL-SEED]" in json.dumps(row) for row in benign)


def test_privileged_probe_plants_a_failed_apply_remediation(attacks):
    probes = [a for a in attacks if a["sentinel"]["attack_type"] == "privileged_probe"]
    for a in probes:
        calls = [t for t in a["tool_trace"] if t["tool"] == "apply_remediation"]
        assert calls and all(t["ok"] is False for t in calls)


def test_budget_abuse_inflates_calls_and_tokens(attacks, benign):
    for a in (x for x in attacks if x["sentinel"]["attack_type"] == "budget_abuse"):
        assert a["tool_calls"] >= 30
        assert a["tokens"]["output"] >= 40_000


def test_exfil_fans_out_far_beyond_a_normal_run(attacks):
    for a in (x for x in attacks if x["sentinel"]["attack_type"] == "exfil_shaped"):
        fetches = [t for t in a["tool_trace"] if t["tool"] == "fetch_resource"]
        assert len(fetches) >= 40


def test_the_manifest_names_only_non_companion_runs():
    manifest = json.loads(corpus.MANIFEST_PATH.read_text())
    selected = set(corpus.select_benign_runs())
    assert set(manifest["runs"]) == selected


def test_cli_stats_reports_both_halves_of_the_stream():
    result = CliRunner().invoke(cli.app, ["corpus", "stats"])
    assert result.exit_code == 0
    assert "benign rows" in result.output
    assert "attack rows" in result.output


def test_cli_build_reproduces_the_committed_corpus(monkeypatch, tmp_path):
    out = tmp_path / "attacks.jsonl"
    monkeypatch.setattr(corpus, "ATTACKS_PATH", out)
    result = CliRunner().invoke(cli.app, ["corpus", "build"])
    assert result.exit_code == 0
    assert out.read_bytes() == (corpus.CORPUS_DIR / "attacks.jsonl").read_bytes()
