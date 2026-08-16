"""Reconcile (#45): hot vs cold vs oracle, exact or failing.

Hot-vs-cold is the drill's exit criterion; the oracle leg catches the
one failure both stores can share — a message neither ever saw.
"""

import os

import pytest

from resgraph.cold import store as cold_store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.graph.client import get_driver
from resgraph.graph.ingest import apply_batch
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema, wipe
from resgraph.reconcile import reconcile
from resgraph.schema import Op

pytestmark = pytest.mark.integration

SEED, RESOURCES, CHURN = 13, 120, 600


def _oracle(msgs):
    last = {}
    for m in msgs:
        last[m.resource_id] = m
    return {
        rid: {
            "type": m.resource_type.value,
            "attrs": dict(m.attrs),
            "sequence": m.sequence,
            "relationships": sorted((r.type, r.target_id) for r in m.relationships),
        }
        for rid, m in last.items()
        if m.op is Op.UPSERT
    }


@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")
    churn = Churn(World(SEED, RESOURCES))
    snap = list(churn.snapshot())
    more = [churn.next_message() for _ in range(CHURN)]
    msgs = snap + more
    with driver.session() as s:
        wipe(s)
        init_schema(s)
        load_snapshot(s, snap)
        apply_batch(s, more)
        cat = cold_store.get_catalog(tmp_path_factory.mktemp("reconcile-cold"))
        cold_store.ensure_tables(cat)
        cold_store.append_events(cat, msgs)
        yield s, cat, msgs
    driver.close()


def test_reconcile_is_exact_after_identical_feeds(stores):
    session, cat, msgs = stores
    result = reconcile(session, cat, _oracle(msgs))
    assert result["ok"], result
    assert result["hot_count"] == result["cold_count"] == result["oracle_count"]


def test_reconcile_detects_hot_divergence(stores):
    session, cat, msgs = stores
    victim = sorted(_oracle(msgs))[0]
    label = victim.split("-", 1)[0]
    session.run(f"MATCH (n:{label} {{id: $id}}) DETACH DELETE n", id=victim).consume()
    try:
        result = reconcile(session, cat)
        assert not result["ok"]
        assert victim in result["hot_vs_cold"]["only_in_cold"]
    finally:
        wipe(session)
        init_schema(session)
        snap_n = RESOURCES
        load_snapshot(session, msgs[:snap_n])
        apply_batch(session, msgs[snap_n:])


def test_oracle_catches_a_message_both_stores_missed(tmp_path_factory):
    churn = Churn(World(SEED, RESOURCES))
    fed = list(churn.snapshot()) + [churn.next_message() for _ in range(CHURN - 1)]
    tail = churn.next_message()  # the message "evicted" before either store saw it
    oracle = _oracle(fed + [tail])
    cat2 = cold_store.get_catalog(tmp_path_factory.mktemp("reconcile-miss"))
    cold_store.ensure_tables(cat2)
    cold_store.append_events(cat2, fed)
    from resgraph.reconcile import compare, dump_cold

    cold, _ = dump_cold(cat2)
    report = compare(oracle, cold, "oracle", "cold")
    assert not report["ok"]
    affected = tail.resource_id
    assert (
        affected in report["sequence_mismatches"]
        or affected in report["attr_mismatches"]
        or affected in report["only_in_oracle"]
        or affected in report["relationship_mismatches"]
    )


def test_reconcile_detects_an_attribute_divergence(stores):
    session, cat, msgs = stores
    victim = sorted(_oracle(msgs))[0]
    label = victim.split("-", 1)[0]
    session.run(f"MATCH (n:{label} {{id: $id}}) SET n.drifted = 'x'", id=victim).consume()
    try:
        result = reconcile(session, cat)
        assert not result["ok"]
        assert victim in result["hot_vs_cold"]["attr_mismatches"]
    finally:
        wipe(session)
        init_schema(session)
        load_snapshot(session, msgs[:RESOURCES])
        apply_batch(session, msgs[RESOURCES:])
