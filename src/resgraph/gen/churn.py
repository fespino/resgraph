"""Deterministic churn engine — simulated clock (D6), op mix + skew (D7)."""

from datetime import UTC, datetime, timedelta

from resgraph.schema import Op, ResourceType, UpdateMessage

from .world import ATTR_POOLS, CREATION_ORDER, TOPOLOGY, TYPE_MIX, World

WORLD_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# Types that other types depend on (D5) are never churned below a floor —
# otherwise creation/repair pools could empty and violate cardinality lows.
TARGET_FLOOR: dict[ResourceType, int] = {
    ResourceType.HOST: 3,
    ResourceType.SG: 4,
    ResourceType.VM: 3,
    ResourceType.CONTAINER: 3,
    ResourceType.ASG: 2,
}

_REL_SOURCES = tuple(sorted({src for src, _ in TOPOLOGY}, key=lambda t: t.value))
_CREATE_WEIGHTS = [TYPE_MIX[t] for t in CREATION_ORDER]
_REVIVAL_P = 0.3


class Churn:
    def __init__(
        self,
        world: World,
        mean_interval_us: float = 100.0,
        burst_every: float = 0.0,
        burst_len: float = 0.0,
        burst_x: float = 1.0,
    ) -> None:
        self.world = world
        self.seq = 0
        self.now = WORLD_EPOCH
        self.mean = mean_interval_us
        self.burst_every = burst_every
        self.burst_len = burst_len
        self.burst_x = burst_x

    def next_message(self) -> UpdateMessage:
        self.seq += 1
        mean = self.mean
        if self.burst_every > 0:  # D7: burst windows in world time — deterministic
            offset = (self.now - WORLD_EPOCH).total_seconds() % self.burst_every
            if offset < self.burst_len:
                mean = self.mean / self.burst_x
        self.now += timedelta(microseconds=self.world.rng.expovariate(1 / mean))
        roll = self.world.rng.random()
        if roll < 0.03:
            return self._delete()
        if roll < 0.08:
            return self._create()
        if roll < 0.20:
            return self._rel_change()
        return self._attr_update()

    def snapshot(self):
        """One upsert per alive resource — a full world statement."""
        for rid in list(self.world.alive_ids()):
            yield self._emit(self.world.resources[rid], Op.UPSERT)

    def _emit(self, res, op: Op) -> UpdateMessage:
        return UpdateMessage(
            sequence=self.seq,
            event_time=self.now,
            op=op,
            resource_type=res.type,
            resource_id=res.id,
            attrs=dict(res.attrs),
            relationships=list(res.relationships),
        )

    def _attr_update(self) -> UpdateMessage:
        res = self.world.resources[self.world.pick_target()]
        pools = ATTR_POOLS[res.type]
        key = self.world.rng.choice(sorted(pools))
        res.attrs[key] = self.world.rng.choice(pools[key])
        return self._emit(res, Op.UPSERT)

    def _rel_change(self) -> UpdateMessage:
        # Only types that own outbound edges can change them; the message
        # replaces the full edge list (D2: a statement, not a diff).
        for _ in range(8):
            res = self.world.resources[self.world.pick_target()]
            if res.type in _REL_SOURCES:
                self.world.set_relationships(res, self.world.roll_relationships(res.type))
                return self._emit(res, Op.UPSERT)
        return self._attr_update()

    def _create(self) -> UpdateMessage:
        t = self.world.rng.choices(CREATION_ORDER, weights=_CREATE_WEIGHTS)[0]
        revivable = self.world.deleted_ids(t)
        if revivable and self.world.rng.random() < _REVIVAL_P:  # D3 revival path
            res = self.world.create(t, resource_id=self.world.rng.choice(revivable))
        else:
            res = self.world.create(t)
        return self._emit(res, Op.UPSERT)

    def _delete(self) -> UpdateMessage:
        for _ in range(8):
            rid = self.world.pick_target()
            t = self.world.resources[rid].type
            if self.world.alive_count(t) > TARGET_FLOOR.get(t, 1):
                return self._emit(self.world.delete(rid), Op.DELETE)
        return self._attr_update()
