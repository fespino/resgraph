"""Property suite for the generator — the D5/D6/D7 invariants, across seeds."""

from hypothesis import given, settings
from hypothesis import strategies as st

from resgraph.gen.churn import Churn
from resgraph.gen.world import TOPOLOGY, World
from resgraph.schema import Op, UpdateMessage

RESOURCES = 400
N = 1000


def take(seed: int, n: int = N) -> list[UpdateMessage]:
    churn = Churn(World(seed, RESOURCES))
    return [churn.next_message() for _ in range(n)]


def stepped(seed: int, n: int = N):
    """Yield (message, world) pairs so invariants can be checked at emit time."""
    churn = Churn(World(seed, RESOURCES))
    for _ in range(n):
        yield churn.next_message(), churn.world


@given(seed=st.integers(0, 2**32 - 1))
@settings(max_examples=15, deadline=None)
def test_stream_deterministic(seed):
    a = [m.model_dump_json() for m in take(seed)]
    b = [m.model_dump_json() for m in take(seed)]
    assert a == b  # D6: byte-identical


@given(seed=st.integers(0, 2**32 - 1))
@settings(max_examples=15, deadline=None)
def test_sequence_strictly_monotonic(seed):
    seqs = [m.sequence for m in take(seed)]
    assert seqs == sorted(set(seqs))


@given(seed=st.integers(0, 2**32 - 1))
@settings(max_examples=10, deadline=None)
def test_relationship_targets_alive_at_emit(seed):
    for msg, world in stepped(seed):
        for rel in msg.relationships:
            target = world.resources[rel.target_id]
            assert not target.deleted, (msg.resource_id, rel)


@given(seed=st.integers(0, 2**32 - 1))
@settings(max_examples=10, deadline=None)
def test_topology_respected(seed):
    for msg, world in stepped(seed):
        if msg.op is Op.DELETE:
            assert not msg.attrs and not msg.relationships
            continue
        counts: dict[str, int] = {}
        for rel in msg.relationships:
            key = (msg.resource_type, rel.type)
            assert key in TOPOLOGY, key  # D5: edge type allowed for this source
            allowed, _, _ = TOPOLOGY[key]
            assert world.resources[rel.target_id].type in allowed, rel
            counts[rel.type] = counts.get(rel.type, 0) + 1
        for (src, rel_type), (_, lo, hi) in TOPOLOGY.items():
            if src is not msg.resource_type:
                continue
            assert lo <= counts.get(rel_type, 0) <= hi, (msg.resource_id, rel_type)


@given(seed=st.integers(0, 2**32 - 1))
@settings(max_examples=10, deadline=None)
def test_messages_valid_on_wire(seed):
    for msg in take(seed, n=200):
        assert UpdateMessage.model_validate_json(msg.model_dump_json()) == msg
