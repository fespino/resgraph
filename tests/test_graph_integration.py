"""Golden tests against a real Memgraph — fixture world seed=42, n=500.

The generator is deterministic (D6), so expected values are golden
constants: computed once, eyeballed against the world, then pinned.
Skipped when no store is reachable; CI sets RESGRAPH_REQUIRE_STORES=1
so absence fails loudly there instead.
"""

import os

import pytest

from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.graph import queries
from resgraph.graph.client import cypher, get_driver
from resgraph.graph.loader import StoreNotEmptyError, load_snapshot
from resgraph.graph.schema import init_schema, node_count, wipe

pytestmark = pytest.mark.integration

SEED, RESOURCES = 42, 500


@pytest.fixture(scope="session")
def session():
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")
    with driver.session() as s:
        yield s
    driver.close()


@pytest.fixture(scope="session")
def world(session):
    s = session
    wipe(s)
    init_schema(s)
    w = World(SEED, RESOURCES)
    counts = load_snapshot(s, Churn(w).snapshot())
    assert counts["nodes"] == len(w.alive_ids())
    return w


def test_load_counts_match_world(session, world):
    per_type = {r["type"]: r["total"] for r in queries.stats(session)["nodes"]}
    for t, ids in ((t, world._alive_by_type[t]) for t in world._alive_by_type):
        if ids:
            assert per_type[t.value] == len(ids)
    # a clean snapshot has no phantoms — every target existed at emit
    assert all(r["phantom"] == 0 for r in queries.stats(session)["nodes"])


def test_loader_refuses_non_empty(session, world):
    assert node_count(session) > 0
    with pytest.raises(StoreNotEmptyError):
        load_snapshot(session, [])


def test_blast_radius_golden(session, world):
    got = [a.id for a in queries.blast_radius(session, GOLDEN_HOST, depth=3)]
    assert got == GOLDEN_BLAST_RADIUS


def test_blast_radius_is_node_set_not_path_count(session, world):
    """The DISTINCT regression: variable-length expansion counts paths;
    the blast radius is a set of nodes. Constructed diamond (an lb routing
    to two vms on one host = two paths, one dependent) because at this
    seed no fixture lb happens to multi-path — found that out by
    measuring, which is the point of the test."""
    # Real type prefixes (the anchor label is derived from them, D8), with
    # a -zz suffix that no generated id uses.
    session.run(
        """
        CREATE (h:host {id: 'host-zz0', deleted: false})
        CREATE (v1:vm {id: 'vm-zz1', deleted: false})-[:RUNS_ON]->(h)
        CREATE (v2:vm {id: 'vm-zz2', deleted: false})-[:RUNS_ON]->(h)
        CREATE (l:lb {id: 'lb-zz3', deleted: false})
        CREATE (l)-[:ROUTES_TO]->(v1)
        CREATE (l)-[:ROUTES_TO]->(v2)
        """
    ).consume()
    try:
        ids = [a.id for a in queries.blast_radius(session, "host-zz0", depth=3)]
        assert ids == ["lb-zz3", "vm-zz1", "vm-zz2"]  # a set: lb once
        path_rows = cypher(
            session,
            f"""
            MATCH (x:host {{id: 'host-zz0'}})<-[{queries.DEP_EDGES} *1..3]-(dep)
            RETURN dep.id AS id
            """,
        )
        assert len(path_rows) == 4  # v1, v2, and the lb TWICE — a path count
    finally:
        session.run("MATCH (n) WHERE n.id CONTAINS '-zz' DETACH DELETE n").consume()


def test_depth_cap_is_the_injection_guard(session, world):
    for bad in (0, 7, -1):
        with pytest.raises(ValueError):
            queries.blast_radius(session, GOLDEN_HOST, depth=bad)
    with pytest.raises((ValueError, TypeError)):
        queries.blast_radius(session, GOLDEN_HOST, depth="3}]-() MATCH (n) DETACH DELETE n //")


def test_anchor_label_derivation_guards_injection(session, world):
    # the label lands in the query string; an unknown/crafted prefix is
    # rejected before it gets there (D8 index requires the label anyway)
    for bad in ("evil-1", "vm`) DETACH DELETE (n)-1", "-000001"):
        with pytest.raises(ValueError):
            queries.blast_radius(session, bad, depth=2)


def test_dependency_path_golden(session, world):
    p = queries.dependency_path(session, GOLDEN_CONTAINER, GOLDEN_CONTAINER_HOST)
    assert p is not None
    assert p.path[0] == GOLDEN_CONTAINER and p.path[-1] == GOLDEN_CONTAINER_HOST
    assert p.rels == ["RUNS_ON", "RUNS_ON"]  # container -> vm -> host


def test_orphans_clean_then_induced(session, world):
    assert queries.orphans(session) == []
    # induce: tombstone one vm that containers run on, then restore
    victim = GOLDEN_ORPHAN_VM
    session.run("MATCH (n {id: $id}) SET n.deleted = true", id=victim).consume()
    try:
        got = queries.orphans(session)
        assert got and all(r["missing_dependency"] == victim for r in got)
        assert [r["id"] for r in got] == GOLDEN_ORPHANED_CONTAINERS
    finally:
        session.run("MATCH (n {id: $id}) SET n.deleted = false", id=victim).consume()


# --- golden constants (seed=42, resources=500; D6 makes these stable) ---
# Pinned after computing them two independent ways (world reverse-deps
# walk vs store BFS) and requiring agreement — the automated eyeball.
GOLDEN_HOST = "host-000015"  # the hub host: largest depth-3 blast radius
GOLDEN_BLAST_RADIUS = [
    "container-000010",
    "container-000022",
    "container-000025",
    "container-000035",
    "container-000050",
    "container-000053",
    "container-000064",
    "container-000066",
    "container-000071",
    "container-000097",
    "container-000098",
    "container-000101",
    "container-000102",
    "container-000114",
    "container-000117",
    "container-000131",
    "container-000179",
    "db-000020",
    "lb-000003",
    "lb-000008",
    "lb-000015",
    "lb-000017",
    "lb-000022",
    "lb-000023",
    "vm-000017",
    "vm-000025",
    "vm-000030",
    "vm-000093",
    "vm-000094",
    "vm-000108",
    "vm-000118",
    "vm-000119",
    "vm-000129",
    "vm-000141",
    "vm-000142",
    "vm-000143",
]
GOLDEN_CONTAINER = "container-000001"
GOLDEN_CONTAINER_HOST = "host-000018"  # via vm-000047
GOLDEN_ORPHAN_VM = "vm-000074"  # most containers run on it
GOLDEN_ORPHANED_CONTAINERS = [
    "container-000095",
    "container-000120",
    "container-000122",
    "container-000124",
    "container-000133",
    "container-000166",
]
