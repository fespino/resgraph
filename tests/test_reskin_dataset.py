"""The committed re-skin companion set (#103): every item preserves
its original's causal structure, changes its surface, rebuilds from
its recipe, and records lineage. The sanitization sweep covers this
file automatically (it globs the datasets directory)."""

from pathlib import Path

from resgraph.gen.scenarios import Scenario, rebuild

BASE = Path("evals/scenarios/base.jsonl")
RESKIN = Path("evals/scenarios/reskin-100k.jsonl")


def load(path):
    return [
        Scenario.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()
    ]


def test_reskins_preserve_structure_and_change_surface():
    base = {s.id: s for s in load(BASE)}
    items = load(RESKIN)
    assert len(items) == len(base)
    for item in items:
        original = base[str(item.provenance["reskin_of"])]
        assert item.id != original.id
        assert item.scenario_type is original.scenario_type
        assert item.depth == original.depth
        assert item.seed == original.seed + 100_000
        assert "reskin" in item.tags
        if item.ground_truth is not None:
            assert original.ground_truth is not None
            assert item.ground_truth.mechanism_path != original.ground_truth.mechanism_path
        rebuild(item)
