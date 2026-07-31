"""Property tests pinning the ingest apply semantics against a real store.

The apply rule in one line: apply iff ``msg.sequence > node.applied_seq``,
with the watermark check and the write in one transaction. These tests
pin its consequences as executable contracts, before the apply path
exists:

  - replaying any message is a no-op (idempotent);
  - out-of-order-within-a-resource is safe (stale skipped);
  - a delete tombstones (node kept, ``deleted=true``) and a
    higher-sequence upsert revives it;
  - dangling targets become phantom placeholders;
  - CONVERGENCE: applying a resource's messages in ANY order — and
    replaying the whole set — yields the same final node state, the one
    implied by the single highest-sequence message.

Convergence is the reason the watermark exists; the rest are corollaries.
Skipped when no store is reachable; CI sets RESGRAPH_REQUIRE_STORES=1 so
absence fails loudly there instead.
"""

import os
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resgraph.gen.churn import WORLD_EPOCH
from resgraph.graph import ingest
from resgraph.graph.client import get_driver
from resgraph.graph.schema import init_schema, node_count
from resgraph.schema import Op, Relationship, UpdateMessage

pytestmark = pytest.mark.integration

# All test nodes carry the "-d3" sentinel so a single cleanup reaches the
# source and every phantom/target it spawns, without touching a fixture
# world that may share the store.
SOURCE = "vm-d3src"
TARGETS = [f"host-d3t{i}" for i in range(4)]
REL_TYPES = ["runs_on", "attached_to", "routes_to", "member_of"]
ATTR_KEYS = ["zone", "state", "cpu", "tier"]


@pytest.fixture(scope="module")
def session():
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")
    with driver.session() as s:
        init_schema(s)
        yield s
        _reset(s)
    driver.close()


def _reset(session) -> None:
    session.run("MATCH (n) WHERE n.id CONTAINS '-d3' DETACH DELETE n").consume()


# --- message builders --------------------------------------------------

def _upsert(seq: int, attrs: dict, rels: list[tuple[str, str]]) -> UpdateMessage:
    return UpdateMessage(
        sequence=seq,
        event_time=WORLD_EPOCH + timedelta(seconds=seq),
        op=Op.UPSERT,
        resource_type="vm",
        resource_id=SOURCE,
        attrs=dict(attrs),
        relationships=[Relationship(type=t, target_id=tid) for t, tid in rels],
    )


def _delete(seq: int) -> UpdateMessage:
    return UpdateMessage(
        sequence=seq,
        event_time=WORLD_EPOCH + timedelta(seconds=seq),
        op=Op.DELETE,
        resource_type="vm",
        resource_id=SOURCE,
    )


# --- oracle: the final state implied by the highest-sequence message ---

def _expected(final: UpdateMessage) -> dict:
    """The whole point of the watermark: state is a pure function of the
    single highest-sequence message, so the oracle ignores everything
    else."""
    if final.op is Op.DELETE:
        # tombstone carries no payload — attrs cleared, edges dropped —
        # which is exactly what makes a max-seq delete order-independent.
        return {"applied_seq": final.sequence, "deleted": True,
                "deleted_seq": final.sequence, "attrs": {}, "rels": []}
    rels = sorted({(r.type.upper(), r.target_id) for r in final.relationships})
    return {"applied_seq": final.sequence, "deleted": False,
            "attrs": dict(final.attrs), "rels": rels}


def _assert_state(session, expected: dict) -> None:
    got = ingest.read_node(session, SOURCE)
    assert got is not None, "node must exist (a tombstone is not a removal)"
    assert got["applied_seq"] == expected["applied_seq"]
    assert got["deleted"] == expected["deleted"]
    assert got["attrs"] == expected["attrs"]
    assert got["rels"] == expected["rels"]
    if expected["deleted"]:
        assert got["deleted_seq"] == expected["deleted_seq"]


# --- explicit corollaries ---------------------------------------------

def test_replay_is_a_noop(session):
    _reset(session)
    m = _upsert(5, {"zone": "z1", "cpu": 4}, [("runs_on", TARGETS[0])])
    assert ingest.apply_message(session, m) is True
    before = ingest.read_node(session, SOURCE)
    assert ingest.apply_message(session, m) is False  # watermark skips
    assert ingest.read_node(session, SOURCE) == before


def test_out_of_order_lower_sequence_is_skipped(session):
    _reset(session)
    assert ingest.apply_message(session, _upsert(5, {"zone": "z1"}, [])) is True
    # a stale message that arrives late must not overwrite
    assert ingest.apply_message(session, _upsert(3, {"zone": "z9"}, [])) is False
    _assert_state(session, _expected(_upsert(5, {"zone": "z1"}, [])))


def test_delete_tombstones_then_higher_seq_upsert_revives(session):
    _reset(session)
    ingest.apply_message(session, _upsert(1, {"zone": "z1"}, [("runs_on", TARGETS[0])]))
    assert ingest.apply_message(session, _delete(2)) is True
    tomb = ingest.read_node(session, SOURCE)
    assert tomb is not None and tomb["deleted"] is True  # kept, not removed
    assert tomb["rels"] == []  # outbound edges dropped on delete
    revive = _upsert(3, {"zone": "z2"}, [("runs_on", TARGETS[1])])
    assert ingest.apply_message(session, revive) is True
    _assert_state(session, _expected(revive))


def test_stale_upsert_cannot_resurrect_a_tombstone(session):
    _reset(session)
    ingest.apply_message(session, _upsert(1, {"zone": "z1"}, []))
    ingest.apply_message(session, _delete(3))
    # an upsert older than the delete must stay skipped — tombstone holds
    assert ingest.apply_message(session, _upsert(2, {"zone": "z2"}, [])) is False
    assert ingest.read_node(session, SOURCE)["deleted"] is True


def test_dangling_target_becomes_phantom_then_resolves(session):
    _reset(session)
    ingest.apply_message(session, _upsert(1, {}, [("runs_on", TARGETS[0])]))
    tgt = ingest.read_node(session, TARGETS[0])
    assert tgt is not None and tgt["phantom"] is True  # created, not dropped
    # the target's own upsert clears the phantom flag; the edge survives
    host_upsert = UpdateMessage(
        sequence=2,
        event_time=WORLD_EPOCH + timedelta(seconds=2),
        op=Op.UPSERT,
        resource_type="host",
        resource_id=TARGETS[0],
        attrs={"zone": "z1"},
    )
    ingest.apply_message(session, host_upsert)
    assert ingest.read_node(session, TARGETS[0])["phantom"] is False
    assert ("RUNS_ON", TARGETS[0]) in ingest.read_node(session, SOURCE)["rels"]


# --- the convergence property -----------------------------------------

_attr_values = st.one_of(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", max_size=6),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
)
_attrs = st.dictionaries(st.sampled_from(ATTR_KEYS), _attr_values, max_size=4)
_rels = st.lists(
    st.tuples(st.sampled_from(REL_TYPES), st.sampled_from(TARGETS)), max_size=4
)


@st.composite
def _events_and_order(draw):
    """A resource's message history (canonical sequence = list index + 1)
    plus an arbitrary arrival permutation of it."""
    kinds = draw(st.lists(st.sampled_from(["upsert", "delete"]),
                          min_size=1, max_size=8))
    events = []
    for i, kind in enumerate(kinds):
        seq = i + 1
        events.append(_delete(seq) if kind == "delete"
                      else _upsert(seq, draw(_attrs), draw(_rels)))
    order = draw(st.permutations(range(len(events))))
    return events, order


@settings(max_examples=40, deadline=None)
@given(spec=_events_and_order())
def test_apply_is_order_independent_and_idempotent(session, spec):
    """Convergence: any arrival order, plus a full replay, lands on the
    state implied by the single highest-sequence message."""
    events, order = spec
    _reset(session)
    expected = _expected(events[-1])  # highest sequence == last built

    for i in order:
        ingest.apply_message(session, events[i])
    _assert_state(session, expected)

    # replay the entire history in canonical order — must change nothing
    for m in events:
        assert ingest.apply_message(session, m) is False
    _assert_state(session, expected)


@settings(max_examples=15, deadline=None)
@given(spec=_events_and_order())
def test_a_tombstone_is_never_a_removal(session, spec):
    """However the messages interleave, the resource node is always
    present afterwards — history is kept via tombstones, not deletes."""
    events, order = spec
    _reset(session)
    for i in order:
        ingest.apply_message(session, events[i])
    assert node_count(session) >= 1
    assert ingest.read_node(session, SOURCE) is not None
