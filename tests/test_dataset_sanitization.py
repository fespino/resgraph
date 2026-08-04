"""CI sweep of the mechanical sanitization validators over every
committed dataset (SANITIZATION.md). The miner runs the same
validators at creation time; this sweep re-checks the committed
files, so items hand-edited past the miner are caught and the check
does not trust its author."""

from pathlib import Path

import pytest

from resgraph.evals.sanitize import (
    Finding,
    local_env,
    model_names,
    sanitize_findings,
    secrets,
)
from resgraph.gen.scenarios import Scenario

DATASETS = sorted(Path("evals/scenarios").glob("*.jsonl"))


@pytest.mark.parametrize("dataset", DATASETS, ids=[p.name for p in DATASETS])
def test_committed_dataset_is_sanitized(dataset):
    for line in dataset.read_text().splitlines():
        if not line.strip():
            continue
        spec = Scenario.model_validate_json(line)
        findings = sanitize_findings(spec)
        assert not findings, f"{spec.id}: " + "; ".join(str(f) for f in findings)


def test_validators_fire_on_known_bad_text():
    assert secrets.scan("api_key=abc123 and Bearer abcdefghijklmnop.qrst")
    assert secrets.scan("AKIAABCDEFGHIJKLMNOP")[0].startswith("aws-access-key")
    assert local_env.scan("/Users/someone/code/resgraph")
    assert local_env.scan("box-3.local")
    assert model_names.scan("failed on opus and Claude")


def test_secret_findings_never_echo_the_match():
    token = "sk-ant-abcdef1234567890"
    details = secrets.scan(f"leaked {token}")
    assert details
    assert all(token not in detail for detail in details)


def test_findings_name_validator_and_field():
    spec = Scenario.model_validate_json(
        Path("evals/scenarios/regression.jsonl").read_text().splitlines()[0]
    )
    dirty = spec.model_copy(
        update={"provenance": dict(spec.provenance) | {"note": "run at /Users/dev/x on opus"}}
    )
    findings = sanitize_findings(dirty)
    assert {f.validator for f in findings} == {"local-env", "model-name"}
    assert all(isinstance(f, Finding) and f.field == "provenance.note" for f in findings)


def test_lineage_findings_on_incomplete_derivation():
    spec = Scenario.model_validate_json(
        Path("evals/scenarios/base.jsonl").read_text().splitlines()[0]
    )
    broken = spec.model_copy(
        update={"provenance": dict(spec.provenance) | {"source": "failure_derived"}}
    )
    validators = [f.validator for f in sanitize_findings(broken)]
    assert validators.count("lineage") == 3
