"""The committed regression dataset (D24: failure-derived items are
permanent): every item rebuilds, carries its lineage, and collides
with nothing in the base set."""

from pathlib import Path

from resgraph.gen.scenarios import Scenario, rebuild

BASE = Path("evals/scenarios/base.jsonl")
REGRESSION = Path("evals/scenarios/regression.jsonl")


def load(path):
    return [
        Scenario.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()
    ]


def test_regression_items_rebuild_with_lineage_and_distinct_ids():
    base_ids = {s.id for s in load(BASE)}
    items = load(REGRESSION)
    assert items, "regression dataset is empty"
    seen = set()
    for item in items:
        assert item.id not in base_ids and item.id not in seen
        seen.add(item.id)
        prov = item.provenance
        assert prov["source"] == "failure_derived"
        assert prov["derived_from"] in base_ids
        assert str(prov["exposed_by_run"])
        assert prov["bucket"] in ("early_conclusion", "decoy_seduction")
        assert "regression" in item.tags
        rebuild(item)


def test_base_items_are_planted():
    for spec in load(BASE):
        assert spec.provenance["source"] == "planted"
