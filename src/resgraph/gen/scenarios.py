"""Causal-scenario planting — the ground truth is known because the
generator planted it, never inferred after the fact.

A scenario is a deterministic message stream (seeded world, steady
churn, one planted causal change, distractor changes drawn from the
same attribute pools as causes, an alert downstream of the cause) plus
the ground truth as data. The mechanism path is selected and asserted
against the log's own as-of state — highest sequence per resource with
event_time <= t, deletes mean absent — which is the cold store's
answer recomputed from the stream; a golden test pins that agreement.

Path nodes must be alive in the log at planting time: mechanism paths
through phantom nodes are deliberately out of scope at v1.

The `description` field is for dataset readers; an agent under
evaluation sees only the alert.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Self, assert_never

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from resgraph.schema import Op, ResourceType, UpdateMessage

from .churn import TARGET_FLOOR, Churn
from .world import ATTR_POOLS, World

STEADY_CHURN = 40
DISTRACTORS = 4
NOISY_DISTRACTORS = 12
MAX_ATTEMPTS = 20
# Worlds without the required structure retry under a re-derived seed;
# the stride keeps retry seeds far from neighboring base seeds.
_ATTEMPT_STRIDE = 100_003

SYMPTOMS: dict[ResourceType, str] = {
    ResourceType.CONTAINER: "crash_loop",
    ResourceType.DB: "connection_errors",
    ResourceType.LB: "latency_high",
    ResourceType.VM: "unreachable",
}

# Fault values come from ATTR_POOLS, so causes and distractors share
# surface templates: nothing about a change's shape says "planted".
FAULT_CHOICES: dict[ResourceType, tuple[tuple[str, str | int | bool], ...]] = {
    ResourceType.ASG: (("desired", 1), ("min", 0)),
    ResourceType.CONTAINER: (("state", "exited"), ("state", "restarting")),
    ResourceType.DB: (("state", "degraded"), ("connections", 200)),
    ResourceType.HOST: (("state", "down"), ("state", "maintenance")),
    ResourceType.LB: (("state", "draining"), ("healthy_targets", 0)),
    ResourceType.SG: (("open_to_world", True), ("rule_count", 25)),
    ResourceType.VM: (("state", "stopped"), ("state", "starting")),
}


class ScenarioType(StrEnum):
    AMBIGUOUS = "ambiguous"
    CONTROL = "control"
    DECOY = "decoy"
    DELETED_RESOURCE = "deleted_resource"
    DIRECT = "direct"
    NOISY_WINDOW = "noisy_window"
    TRANSITIVE = "transitive"


def _default_depth(scenario_type: ScenarioType) -> int:
    match scenario_type:
        case (
            ScenarioType.AMBIGUOUS
            | ScenarioType.DELETED_RESOURCE
            | ScenarioType.DIRECT
            | ScenarioType.NOISY_WINDOW
        ):
            return 1
        case ScenarioType.DECOY | ScenarioType.TRANSITIVE:
            return 2
        case ScenarioType.CONTROL:
            raise ValueError("control scenarios have no planted depth")
    return assert_never(scenario_type)


CAUSAL_TYPES = tuple(t for t in ScenarioType if t is not ScenarioType.CONTROL)


class ScenarioError(ValueError):
    pass


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_id: str
    symptom: str
    fired_at: AwareDatetime


class GroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    causal_sequence: int = Field(ge=0)
    causal_resource: str
    # Edge chain from cause to alert target; each consecutive pair is a
    # dependency edge (dependent cites the resource before it).
    mechanism_path: list[str] = Field(min_length=2)
    scenario_type: ScenarioType


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    scenario_type: ScenarioType
    seed: int
    n_resources: int
    depth: int | None
    description: str
    alert: Alert
    ground_truth: GroundTruth | None
    provenance: dict[str, str | int]
    tags: list[str]

    @model_validator(mode="after")
    def _shape_matches_type(self) -> Self:
        is_control = self.scenario_type is ScenarioType.CONTROL
        if (self.ground_truth is None) != is_control or (self.depth is None) != is_control:
            raise ValueError("ground_truth and depth are absent exactly for controls")
        if self.ground_truth is not None:
            if self.ground_truth.scenario_type is not self.scenario_type:
                raise ValueError("ground_truth.scenario_type must match scenario_type")
            if len(self.ground_truth.mechanism_path) != (self.depth or 0) + 1:
                raise ValueError("mechanism_path length must be depth + 1")
        return self


@dataclass
class GeneratedScenario:
    spec: Scenario
    messages: list[UpdateMessage]


def log_state(messages: list[UpdateMessage], t: datetime | None = None) -> dict[str, UpdateMessage]:
    """Winning upsert per resource at t — the as-of answer, from the log."""
    winners: dict[str, UpdateMessage] = {}
    for m in messages:
        if t is not None and m.event_time > t:
            continue
        cur = winners.get(m.resource_id)
        if cur is None or m.sequence > cur.sequence:
            winners[m.resource_id] = m
    return {rid: m for rid, m in winners.items() if m.op is Op.UPSERT}


def edges_at(messages: list[UpdateMessage], t: datetime | None = None) -> set[tuple[str, str]]:
    """(dependent, dependency) pairs asserted by the winning upserts at t.
    Edges to deleted resources are kept: a survivor's statement stands
    until it re-states, which is what makes a deleted cause findable."""
    return {
        (m.resource_id, r.target_id)
        for m in log_state(messages, t).values()
        for r in m.relationships
    }


def assert_mechanism_path(messages: list[UpdateMessage], path: list[str], t: datetime) -> None:
    edges = edges_at(messages, t)
    for dependency, dependent in pairwise(path):
        if (dependent, dependency) not in edges:
            raise ScenarioError(
                f"mechanism edge {dependent}->{dependency} absent at {t.isoformat()}"
            )


def _rtype(rid: str) -> ResourceType:
    return ResourceType(rid.split("-", 1)[0])


def _step(churn: Churn) -> None:
    churn.seq += 1
    churn.now += timedelta(microseconds=churn.world.rng.expovariate(1 / churn.mean))


def _emit(churn: Churn, rid: str, op: Op = Op.UPSERT) -> UpdateMessage:
    res = churn.world.resources[rid]
    return UpdateMessage(
        sequence=churn.seq,
        event_time=churn.now,
        op=op,
        resource_type=res.type,
        resource_id=res.id,
        attrs=dict(res.attrs),
        relationships=list(res.relationships),
    )


def _plant_fault(churn: Churn, rid: str) -> UpdateMessage:
    _step(churn)
    res = churn.world.resources[rid]
    for key, value in FAULT_CHOICES[res.type]:
        if res.attrs.get(key) != value:
            res.attrs[key] = value
            break
    return _emit(churn, rid)


def _distract(churn: Churn, rid: str) -> UpdateMessage:
    _step(churn)
    res = churn.world.resources[rid]
    pools = ATTR_POOLS[res.type]
    key = churn.world.rng.choice(sorted(pools))
    res.attrs[key] = churn.world.rng.choice(pools[key])
    return _emit(churn, rid)


def _deletable(world: World, rid: str) -> bool:
    t = _rtype(rid)
    return world.alive_count(t) > TARGET_FLOOR.get(t, 1)


def _alive_edges(state: dict[str, UpdateMessage]) -> dict[str, list[str]]:
    return {
        rid: sorted({r.target_id for r in m.relationships if r.target_id in state})
        for rid, m in state.items()
    }


def _closure(edges: dict[str, list[str]], start: str, depth: int) -> set[str]:
    seen = {start}
    frontier = [start]
    for _ in range(depth):
        frontier = [t for rid in frontier for t in edges.get(rid, ()) if t not in seen]
        seen.update(frontier)
    return seen


def _walk_path(
    world: World,
    state: dict[str, UpdateMessage],
    depth: int,
    *,
    cause_deletable: bool = False,
    need_rival: bool = False,
) -> tuple[list[str], str | None] | None:
    """A dependency chain [cause, ..., alert target], all alive in the log."""
    edges = _alive_edges(state)
    starts = [rid for rid in sorted(state) if _rtype(rid) in SYMPTOMS]
    for start in world.rng.sample(starts, len(starts)):
        nodes = [start]
        for _ in range(depth):
            pool = [t for t in edges.get(nodes[-1], ()) if t not in nodes]
            if not pool:
                break
            nodes.append(world.rng.choice(pool))
        if len(nodes) != depth + 1:
            continue
        cause = nodes[-1]
        if cause_deletable and not _deletable(world, cause):
            continue
        rival = None
        if need_rival:
            rival_pool = [t for t in edges[start] if t not in nodes]
            if not rival_pool:
                continue
            rival = world.rng.choice(rival_pool)
        return list(reversed(nodes)), rival
    return None


def generate_scenario(
    scenario_type: ScenarioType,
    seed: int,
    n_resources: int = 60,
    depth: int | None = None,
) -> GeneratedScenario:
    last: ScenarioError | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return _generate(scenario_type, seed, n_resources, depth, attempt)
        except ScenarioError as e:
            last = e
    raise ScenarioError(
        f"no {scenario_type.value} structure found for seed {seed} "
        f"after {MAX_ATTEMPTS} attempts: {last}"
    )


def _generate(
    scenario_type: ScenarioType,
    seed: int,
    n_resources: int,
    depth: int | None,
    attempt: int,
) -> GeneratedScenario:
    world = World(seed + attempt * _ATTEMPT_STRIDE, n_resources)
    churn = Churn(world)
    messages = list(churn.snapshot())
    messages += [churn.next_message() for _ in range(STEADY_CHURN)]
    state = log_state(messages)

    n_distract = NOISY_DISTRACTORS if scenario_type is ScenarioType.NOISY_WINDOW else DISTRACTORS
    provenance: dict[str, str | int] = {
        "generator": "resgraph-gen scenario",
        "attempt": attempt,
        "steady_churn": STEADY_CHURN,
        "distractors": n_distract,
    }

    if scenario_type is ScenarioType.CONTROL:
        return _generate_control(
            scenario_type, seed, n_resources, churn, messages, state, n_distract, provenance
        )

    want_depth = depth if depth is not None else _default_depth(scenario_type)
    if not 1 <= want_depth <= 3:
        raise ValueError(f"depth must be 1..3, got {want_depth}")
    found = _walk_path(
        world,
        state,
        want_depth,
        cause_deletable=scenario_type is ScenarioType.DELETED_RESOURCE,
        need_rival=scenario_type is ScenarioType.AMBIGUOUS,
    )
    if found is None:
        raise ScenarioError(f"no depth-{want_depth} dependency chain in this world")
    path, rival = found
    cause, target = path[0], path[-1]

    decoy = None
    if scenario_type is ScenarioType.DECOY:
        off_path = sorted(set(state) - _closure(_alive_edges(state), target, 3))
        decoy_pool = [rid for rid in off_path if "state" in ATTR_POOLS[_rtype(rid)]]
        if not decoy_pool:
            raise ScenarioError("no off-path decoy candidate")
        decoy = world.rng.choice(decoy_pool)

    if scenario_type is ScenarioType.DELETED_RESOURCE:
        _step(churn)
        world.delete(cause)
        cause_msg = _emit(churn, cause, Op.DELETE)
    else:
        cause_msg = _plant_fault(churn, cause)
    messages.append(cause_msg)

    excluded = set(path) | {x for x in (rival, decoy) if x}
    pool = [rid for rid in sorted(state) if rid not in excluded]
    picks = world.rng.sample(pool, min(n_distract, len(pool)))
    for i, rid in enumerate(picks):
        messages.append(_distract(churn, rid))
        if rival is not None and i == 0:
            messages.append(_plant_fault(churn, rival))
    if decoy is not None:
        messages.append(_plant_fault(churn, decoy))

    _step(churn)
    alert = Alert(resource_id=target, symptom=SYMPTOMS[_rtype(target)], fired_at=churn.now)
    assert_mechanism_path(messages, path, cause_msg.event_time)

    tags = [scenario_type.value, f"depth:{want_depth}"]
    if rival is not None:
        tags.append(f"rival:{rival}")
    if decoy is not None:
        tags.append(f"decoy:{decoy}")
    spec = Scenario(
        id=f"{scenario_type.value}-s{seed}",
        scenario_type=scenario_type,
        seed=seed,
        n_resources=n_resources,
        depth=want_depth,
        description=(
            f"{alert.symptom} on {target}; {scenario_type.value} cause planted on "
            f"{cause} at depth {want_depth} among {len(picks)} distractors"
        ),
        alert=alert,
        ground_truth=GroundTruth(
            causal_sequence=cause_msg.sequence,
            causal_resource=cause,
            mechanism_path=path,
            scenario_type=scenario_type,
        ),
        provenance=provenance,
        tags=tags,
    )
    return GeneratedScenario(spec, messages)


def _generate_control(
    scenario_type: ScenarioType,
    seed: int,
    n_resources: int,
    churn: Churn,
    messages: list[UpdateMessage],
    state: dict[str, UpdateMessage],
    n_distract: int,
    provenance: dict[str, str | int],
) -> GeneratedScenario:
    world = churn.world
    targets = [rid for rid in sorted(state) if _rtype(rid) in SYMPTOMS]
    if not targets:
        raise ScenarioError("no alert-capable resource alive")
    target = world.rng.choice(targets)
    pool = [rid for rid in sorted(state) if rid != target]
    for rid in world.rng.sample(pool, min(n_distract, len(pool))):
        messages.append(_distract(churn, rid))
    _step(churn)
    alert = Alert(resource_id=target, symptom=SYMPTOMS[_rtype(target)], fired_at=churn.now)
    spec = Scenario(
        id=f"{scenario_type.value}-s{seed}",
        scenario_type=scenario_type,
        seed=seed,
        n_resources=n_resources,
        depth=None,
        description=f"{alert.symptom} on {target}; no cause planted, distractors only",
        alert=alert,
        ground_truth=None,
        provenance=provenance,
        tags=[scenario_type.value],
    )
    return GeneratedScenario(spec, messages)


def rebuild(spec: Scenario) -> GeneratedScenario:
    """Regenerate a spec's message stream from its recipe; the dataset
    stores seeds, not streams. A mismatch means the generator changed
    under a committed dataset — that is an error, not a refresh."""
    out = generate_scenario(spec.scenario_type, spec.seed, spec.n_resources, depth=spec.depth)
    if (out.spec.alert, out.spec.ground_truth) != (spec.alert, spec.ground_truth):
        raise ScenarioError(f"rebuild mismatch for {spec.id}")
    return GeneratedScenario(spec, out.messages)


def reskin(spec: Scenario, seed: int) -> GeneratedScenario:
    """Same causal structure, new surface: regenerate under a different
    seed holding type and depth fixed. An agent whose score drops on
    re-skins was reading the generator's templates, not the graph."""
    out = generate_scenario(spec.scenario_type, seed, spec.n_resources, depth=spec.depth)
    provenance = dict(out.spec.provenance) | {"reskin_of": spec.id}
    return GeneratedScenario(out.spec.model_copy(update={"provenance": provenance}), out.messages)


def generate_set(seed: int, count: int = 30, n_resources: int = 60) -> list[GeneratedScenario]:
    """A taxonomy-covering set, ~20% controls, item seeds derived from
    the set seed. Transitive items alternate depth 2 and 3."""
    n_controls = max(1, round(count * 0.2))
    kinds = [ScenarioType.CONTROL] * n_controls + [
        CAUSAL_TYPES[i % len(CAUSAL_TYPES)] for i in range(count - n_controls)
    ]
    out: list[GeneratedScenario] = []
    transitive_seen = 0
    for i, kind in enumerate(kinds):
        depth = None
        if kind is ScenarioType.TRANSITIVE:
            depth = 2 + transitive_seen % 2
            transitive_seen += 1
        out.append(generate_scenario(kind, seed * 1000 + i, n_resources, depth=depth))
    return out
