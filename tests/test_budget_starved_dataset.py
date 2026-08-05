"""The committed budget-starved companion set (#139): one item per
scenario type, tagged so the runner starves the harness. The world is
unchanged from its original — same seed, same planted cause — because
the graded question changes, not the scenario. The sanitization sweep
covers this file automatically (it globs the datasets directory)."""

from pathlib import Path

from resgraph.gen.scenarios import Scenario, rebuild

BASE = Path("evals/scenarios/base.jsonl")
STARVED = Path("evals/scenarios/budget-starved.jsonl")


def load(path):
    return [
        Scenario.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()
    ]


def test_starved_items_reuse_their_original_world():
    base = {s.id: s for s in load(BASE)}
    items = load(STARVED)
    # One per scenario type present in the base set.
    assert {i.scenario_type for i in items} == {s.scenario_type for s in base.values()}
    for item in items:
        original = base[item.id.removesuffix("-bs")]
        assert "budget_starved" in item.tags
        assert item.seed == original.seed  # same world, not a re-skin
        assert item.scenario_type is original.scenario_type
        assert item.ground_truth == original.ground_truth
        rebuild(item)  # the recipe still builds


def test_every_scenario_type_is_covered():
    from resgraph.gen.scenarios import ScenarioType

    items = load(STARVED)
    assert {i.scenario_type for i in items} == set(ScenarioType)
