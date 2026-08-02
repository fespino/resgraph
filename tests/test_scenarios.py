"""Scenario generator invariants — planted truth, taxonomy shape, determinism."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resgraph.gen.scenarios import (
    CAUSAL_TYPES,
    GeneratedScenario,
    ScenarioError,
    ScenarioType,
    assert_mechanism_path,
    edges_at,
    generate_scenario,
    generate_set,
    rebuild,
    reskin,
)
from resgraph.schema import Op, UpdateMessage


def cause_message(g: GeneratedScenario) -> UpdateMessage:
    truth = g.spec.ground_truth
    assert truth is not None
    return next(m for m in g.messages if m.sequence == truth.causal_sequence)


@pytest.mark.parametrize("kind", list(ScenarioType))
def test_deterministic_rebuild(kind):
    a = generate_scenario(kind, 11)
    b = rebuild(a.spec)
    assert [m.model_dump_json() for m in a.messages] == [m.model_dump_json() for m in b.messages]


@given(seed=st.integers(0, 2**16))
@settings(max_examples=10, deadline=None)
def test_deterministic_across_seeds(seed):
    a = generate_scenario(ScenarioType.DIRECT, seed)
    b = generate_scenario(ScenarioType.DIRECT, seed)
    assert a.spec == b.spec
    assert [m.model_dump_json() for m in a.messages] == [m.model_dump_json() for m in b.messages]


@pytest.mark.parametrize("kind", CAUSAL_TYPES)
def test_mechanism_path_exists_at_planting_time(kind):
    g = generate_scenario(kind, 7)
    truth = g.spec.ground_truth
    msg = cause_message(g)
    assert msg.resource_id == truth.causal_resource
    assert_mechanism_path(g.messages, truth.mechanism_path, msg.event_time)


@pytest.mark.parametrize("kind", CAUSAL_TYPES)
def test_alert_is_downstream_and_after_cause(kind):
    g = generate_scenario(kind, 7)
    truth = g.spec.ground_truth
    assert g.spec.alert.resource_id == truth.mechanism_path[-1]
    assert g.spec.alert.resource_id != truth.causal_resource
    assert g.spec.alert.fired_at > cause_message(g).event_time


def test_assert_mechanism_path_rejects_fabricated_edge():
    g = generate_scenario(ScenarioType.DIRECT, 7)
    with pytest.raises(ScenarioError, match="absent"):
        assert_mechanism_path(
            g.messages, ["host-999999", g.spec.alert.resource_id], g.spec.alert.fired_at
        )


def test_control_has_no_truth():
    g = generate_scenario(ScenarioType.CONTROL, 7)
    assert g.spec.ground_truth is None
    assert g.spec.depth is None


def test_deleted_resource_cause_is_a_delete_with_dangling_edge():
    g = generate_scenario(ScenarioType.DELETED_RESOURCE, 7)
    truth = g.spec.ground_truth
    msg = cause_message(g)
    assert msg.op is Op.DELETE
    dependent = truth.mechanism_path[1]
    assert (dependent, truth.causal_resource) in edges_at(g.messages, msg.event_time)


def test_noisy_window_has_ten_plus_distinct_distractors():
    g = generate_scenario(ScenarioType.NOISY_WINDOW, 7)
    t0 = cause_message(g).event_time
    window = {
        m.resource_id
        for m in g.messages
        if t0 < m.event_time <= g.spec.alert.fired_at
        and m.resource_id != g.spec.ground_truth.causal_resource
    }
    assert len(window) >= 10


def test_ambiguous_rival_is_second_dependency_changed_in_window():
    g = generate_scenario(ScenarioType.AMBIGUOUS, 7)
    truth = g.spec.ground_truth
    rival = next(t.split(":", 1)[1] for t in g.spec.tags if t.startswith("rival:"))
    assert rival != truth.causal_resource
    assert (g.spec.alert.resource_id, rival) in edges_at(g.messages)
    t0 = cause_message(g).event_time
    assert any(m.resource_id == rival and m.event_time > t0 for m in g.messages)


def test_decoy_is_off_path_and_last_change_before_alert():
    g = generate_scenario(ScenarioType.DECOY, 7)
    truth = g.spec.ground_truth
    decoy = next(t.split(":", 1)[1] for t in g.spec.tags if t.startswith("decoy:"))
    assert decoy not in truth.mechanism_path
    assert len(truth.mechanism_path) == 3
    edges = edges_at(g.messages)
    reachable = {g.spec.alert.resource_id}
    for _ in range(3):
        reachable |= {b for a, b in edges if a in reachable}
    assert decoy not in reachable
    last = max(g.messages, key=lambda m: m.sequence)
    assert last.resource_id == decoy


@pytest.mark.parametrize("kind", list(ScenarioType))
def test_messages_valid_on_wire_and_monotonic(kind):
    g = generate_scenario(kind, 3)
    planted = [m.sequence for m in g.messages if m.sequence > 0]
    assert planted == sorted(set(planted))
    for m in g.messages[-6:]:
        assert UpdateMessage.model_validate_json(m.model_dump_json()) == m


def test_reskin_preserves_structure_and_changes_surface():
    g = generate_scenario(ScenarioType.TRANSITIVE, 5, depth=3)
    r = reskin(g.spec, seed=6)
    assert r.spec.scenario_type is g.spec.scenario_type
    assert r.spec.depth == g.spec.depth
    assert r.spec.ground_truth.mechanism_path != g.spec.ground_truth.mechanism_path
    assert r.spec.provenance["reskin_of"] == g.spec.id
    rebuild(r.spec)


def test_set_covers_taxonomy_with_control_share():
    items = generate_set(seed=1, count=30)
    kinds = [g.spec.scenario_type for g in items]
    assert len(items) == 30
    assert set(kinds) == set(ScenarioType)
    assert kinds.count(ScenarioType.CONTROL) == 6
    assert len({g.spec.id for g in items}) == 30
    depths = {g.spec.depth for g in items if g.spec.scenario_type is ScenarioType.TRANSITIVE}
    assert depths == {2, 3}
