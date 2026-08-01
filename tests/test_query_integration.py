"""The hot/cold boundary golden test (D15/D16): the composite as-of-T
blast radius must equal the live hot-store blast radius when T = now.

Both stores are fed the identical message sequence — hot through the
loader + ingest apply, cold through append — so any disagreement is a
semantics bug in one of the two routes, and the two paths share no
query code. The strongest cross-store correctness check available, for
free (D6 determinism).
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
from resgraph.query.dsl import parse_filter
from resgraph.query.executor import QueryContext
from resgraph.query.planner import Query, plan

pytestmark = pytest.mark.integration

SEED, RESOURCES, CHURN = 42, 300, 1500


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
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
    with driver.session() as s:
        wipe(s)
        init_schema(s)
        load_snapshot(s, snap)
        apply_batch(s, more)
        cat = cold_store.get_catalog(tmp_path_factory.mktemp("boundary-cold"))
        cold_store.ensure_tables(cat)
        cold_store.append_events(cat, snap + more)
        yield QueryContext(session=s, catalog=cat), (snap + more)
    driver.close()


def _roots(msgs) -> list[str]:
    alive = {}
    for m in msgs:
        alive[m.resource_id] = m.op.value
    ids = sorted(rid for rid, op in alive.items() if op == "upsert")
    return ids[:: max(1, len(ids) // 25)]


def test_composite_at_now_equals_live_blast_radius(ctx):
    qctx, msgs = ctx
    t = msgs[-1].event_time
    disagreements = []
    for root in _roots(msgs):
        live = plan(Query("blast_radius", root=root)).execute(qctx)
        cold = plan(Query("blast_radius", root=root, at=t)).execute(qctx)
        live_ids = sorted(r["id"] for r in live)
        cold_ids = sorted(r["id"] for r in cold)
        if live_ids != cold_ids:
            disagreements.append((root, live_ids, cold_ids))
    assert not disagreements, disagreements


def test_boundary_holds_under_pushdown_and_residual(ctx):
    qctx, msgs = ctx
    t = msgs[-1].event_time
    for flt in ("type=vm", "attrs.zone=z1", "type=container AND attrs.restarts>=2"):
        preds = parse_filter(flt)
        for root in _roots(msgs)[:8]:
            live = plan(Query("blast_radius", root=root, predicates=preds)).execute(qctx)
            cold = plan(Query("blast_radius", root=root, at=t, predicates=preds)).execute(qctx)
            assert {r["id"] for r in live} == {r["id"] for r in cold}, (root, flt)
