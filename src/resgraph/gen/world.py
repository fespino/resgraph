"""Seedable world state + topology rules (D5).

Decisions: D5, D6, D7 (SPEC.md).
"""

from bisect import insort
from dataclasses import dataclass, field
from random import Random

from resgraph.schema import Relationship, RelType, ResourceType

TYPE_MIX: dict[ResourceType, float] = {
    ResourceType.HOST: 0.05,
    ResourceType.VM: 0.30,
    ResourceType.CONTAINER: 0.40,
    ResourceType.DB: 0.05,
    ResourceType.LB: 0.05,
    ResourceType.SG: 0.05,
    ResourceType.ASG: 0.10,
}

# D5 verbatim: (source type, relationship) -> (allowed target types, lo, hi)
TOPOLOGY: dict[tuple[ResourceType, RelType], tuple[tuple[ResourceType, ...], int, int]] = {
    (ResourceType.VM, "runs_on"): ((ResourceType.HOST,), 1, 1),
    (ResourceType.VM, "member_of"): ((ResourceType.ASG,), 0, 1),
    (ResourceType.VM, "attached_to"): ((ResourceType.SG,), 1, 3),
    (ResourceType.CONTAINER, "runs_on"): ((ResourceType.VM,), 1, 1),
    (ResourceType.DB, "runs_on"): ((ResourceType.HOST,), 1, 1),
    (ResourceType.DB, "attached_to"): ((ResourceType.SG,), 1, 2),
    (ResourceType.LB, "routes_to"): ((ResourceType.VM, ResourceType.CONTAINER), 1, 4),
}

# Mutable attribute pools per type — enough entropy for realistic diffs,
# no schema sprawl.
ATTR_POOLS: dict[ResourceType, dict[str, list[str | int | bool]]] = {
    ResourceType.HOST: {
        "state": ["up", "down", "maintenance"],
        "rack": [f"r{i}" for i in range(8)],
        "cpu_count": [16, 32, 64],
    },
    ResourceType.VM: {
        "state": ["running", "stopped", "starting"],
        "cpu": [1, 2, 4, 8],
        "zone": ["z1", "z2", "z3"],
    },
    ResourceType.CONTAINER: {
        "state": ["running", "exited", "restarting"],
        "image_tag": [f"v1.{i}" for i in range(6)],
        "restarts": [0, 1, 2, 3, 5, 8],
    },
    ResourceType.DB: {
        "state": ["available", "backing_up", "degraded"],
        "connections": [0, 5, 20, 80, 200],
        "engine": ["pg15", "pg16"],
    },
    ResourceType.LB: {
        "state": ["active", "draining"],
        "healthy_targets": [0, 1, 2, 3, 4],
        "scheme": ["internal", "external"],
    },
    ResourceType.SG: {
        "rule_count": [1, 2, 5, 10, 25],
        "open_to_world": [False, True],
    },
    ResourceType.ASG: {
        "desired": [1, 2, 3, 5, 8],
        "min": [0, 1],
        "max": [4, 8, 16],
    },
}

# Creation order respects D5 dependencies: targets before their sources.
CREATION_ORDER = [
    ResourceType.HOST,
    ResourceType.SG,
    ResourceType.ASG,
    ResourceType.VM,
    ResourceType.CONTAINER,
    ResourceType.DB,
    ResourceType.LB,
]


@dataclass
class Resource:
    id: str
    type: ResourceType
    attrs: dict[str, str | int | float | bool] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    deleted: bool = False


class World:
    """All randomness flows through self.rng; all iteration is sorted (D6)."""

    def __init__(self, seed: int, n_resources: int) -> None:
        self.rng = Random(seed)
        self.resources: dict[str, Resource] = {}
        self._counters: dict[ResourceType, int] = dict.fromkeys(ResourceType, 0)
        # Sorted alive-id lists, maintained incrementally — pick_target and
        # relationship rolls are the hot path and cannot afford re-sorting.
        self._alive_all: list[str] = []
        self._alive_by_type: dict[ResourceType, list[str]] = {t: [] for t in ResourceType}
        self._deleted_by_type: dict[ResourceType, list[str]] = {t: [] for t in ResourceType}
        # Reverse-dependency index (target_id -> dependent source ids):
        # dangling-edge repair walks dependents, not the whole world —
        # the O(world)-per-delete version was 93% of the first benchmark's
        # runtime (see BENCHMARKS.md).
        self._rdeps: dict[str, set[str]] = {}
        self._seed_world(n_resources)
        n_hot = max(1, len(self._alive_all) // 20)  # D7: hot 5%
        self._hot_set: set[str] = set(self.rng.sample(self._alive_all, n_hot))
        # Hot-alive index maintained incrementally — rebuilding it per
        # message was the first benchmark's bottleneck (see BENCHMARKS.md).
        self._hot_alive: list[str] = sorted(self._hot_set)

    def _seed_world(self, n: int) -> None:
        for t in CREATION_ORDER:
            for _ in range(max(1, round(n * TYPE_MIX[t]))):
                self.create(t)

    def _mint_id(self, t: ResourceType) -> str:
        self._counters[t] += 1
        return f"{t.value}-{self._counters[t]:06d}"

    def alive_ids(self) -> list[str]:
        return self._alive_all

    def alive_of(self, types: tuple[ResourceType, ...]) -> list[str]:
        if len(types) == 1:
            return self._alive_by_type[types[0]]
        merged: list[str] = []
        for t in types:
            merged.extend(self._alive_by_type[t])
        return sorted(merged)

    def alive_count(self, t: ResourceType) -> int:
        return len(self._alive_by_type[t])

    def deleted_ids(self, t: ResourceType) -> list[str]:
        return self._deleted_by_type[t]

    def _index_add(self, src_id: str, rels: list[Relationship]) -> None:
        for rel in rels:
            self._rdeps.setdefault(rel.target_id, set()).add(src_id)

    def _index_remove(self, src_id: str, rels: list[Relationship]) -> None:
        for rel in rels:
            deps = self._rdeps.get(rel.target_id)
            if deps is not None:
                deps.discard(src_id)

    def set_relationships(self, res: Resource, rels: list[Relationship]) -> None:
        self._index_remove(res.id, res.relationships)
        res.relationships = rels
        self._index_add(res.id, rels)

    def create(self, t: ResourceType, resource_id: str | None = None) -> Resource:
        revived = resource_id is not None and resource_id in self.resources
        rid = resource_id or self._mint_id(t)
        attrs: dict[str, str | int | float | bool] = {
            k: self.rng.choice(v) for k, v in sorted(ATTR_POOLS[t].items())
        }
        res = Resource(id=rid, type=t, attrs=attrs, relationships=self.roll_relationships(t))
        self.resources[rid] = res
        self._index_add(rid, res.relationships)
        if revived:
            self._deleted_by_type[t].remove(rid)
            if rid in self._hot_set:
                insort(self._hot_alive, rid)
        insort(self._alive_all, rid)
        insort(self._alive_by_type[t], rid)
        return res

    def delete(self, rid: str) -> Resource:
        res = self.resources[rid]
        self._index_remove(rid, res.relationships)
        res.deleted = True
        res.attrs = {}
        res.relationships = []
        self._alive_all.remove(rid)
        self._alive_by_type[res.type].remove(rid)
        if rid in self._hot_set:
            self._hot_alive.remove(rid)
        insort(self._deleted_by_type[res.type], rid)
        self._repair_dangling(rid)
        return res

    def roll_relationships(self, t: ResourceType) -> list[Relationship]:
        rels: list[Relationship] = []
        for (src, rel_type), (targets, lo, hi) in sorted(
            TOPOLOGY.items(), key=lambda kv: (kv[0][0].value, kv[0][1])
        ):
            if src is not t:
                continue
            pool = self.alive_of(targets)
            k = min(self.rng.randint(lo, hi), len(pool))
            if k > 0:
                rels.extend(
                    Relationship(type=rel_type, target_id=tid)
                    for tid in sorted(self.rng.sample(pool, k))
                )
        return rels

    def _repair_dangling(self, dead_id: str) -> None:
        """D7: dependents' edges to a deleted target are re-rolled in world
        state and surface in each dependent's next emitted message. The
        reverse index makes this O(dependents), not O(world)."""
        for src_id in sorted(self._rdeps.pop(dead_id, ())):
            res = self.resources[src_id]
            repaired: list[Relationship] = []
            for rel in res.relationships:
                if rel.target_id != dead_id:
                    repaired.append(rel)
                    continue
                targets, _, _ = TOPOLOGY[(res.type, rel.type)]
                taken = {r.target_id for r in res.relationships}
                pool = [x for x in self.alive_of(targets) if x not in taken]
                if pool:
                    replacement = Relationship(type=rel.type, target_id=self.rng.choice(pool))
                    repaired.append(replacement)
                    self._rdeps.setdefault(replacement.target_id, set()).add(src_id)
            res.relationships = repaired

    def pick_target(self) -> str:
        """D7: 80% of updates land on the 5% hot set."""
        if self._hot_alive and self.rng.random() < 0.8:
            return self.rng.choice(self._hot_alive)
        return self.rng.choice(self._alive_all)
