"""The canonical bodies and both transport paths, in-process against
live stores — the protocol suite exercises the same code in a
subprocess where coverage cannot see it."""

import asyncio
import os
from datetime import UTC, timedelta

import pytest

from resgraph.cold import store as cold_store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.graph.client import get_driver
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema, wipe
from resgraph.query.executor import QueryContext
from resgraph.tools.canonical.entity import FetchResourceIn, fetch_resource
from resgraph.tools.canonical.history import (
    ResourceHistoryIn,
    WorldDiffIn,
    resource_history,
    world_diff,
)
from resgraph.tools.canonical.traversal import (
    BlastRadiusIn,
    DependencyPathIn,
    blast_radius,
    dependency_path,
)
from resgraph.tools.context import CallerContext

pytestmark = pytest.mark.integration

SEED, RESOURCES = 42, 150


@pytest.fixture(scope="module")
def ctx(tmp_path_factory: pytest.TempPathFactory):
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")
    snap = list(Churn(World(SEED, RESOURCES)).snapshot())
    with driver.session() as s:
        wipe(s)
        init_schema(s)
        load_snapshot(s, snap)
        catalog = cold_store.get_catalog(tmp_path_factory.mktemp("tools-cold"))
        cold_store.ensure_tables(catalog)
        cold_store.append_events(catalog, snap)
        yield (
            CallerContext(
                "mcp", frozenset({"resgraph:read"}), QueryContext(session=s, catalog=catalog)
            ),
            snap,
        )
    driver.close()


def _a_vm(snap) -> str:
    return next(m.resource_id for m in snap if m.resource_type.value == "vm")


def test_fetch_resource_live_and_missing(ctx):
    cctx, snap = ctx
    rid = _a_vm(snap)
    out = fetch_resource(FetchResourceIn(resource_id=rid), ctx=cctx)
    assert out.found and out.source == "hot" and out.attrs
    missing = fetch_resource(FetchResourceIn(resource_id="vm-nope"), ctx=cctx)
    assert not missing.found


def test_fetch_resource_at_reads_the_cold_state(ctx):
    cctx, snap = ctx
    rid = _a_vm(snap)
    t = snap[-1].event_time.replace(tzinfo=UTC) + timedelta(seconds=1)
    out = fetch_resource(FetchResourceIn(resource_id=rid, at=t), ctx=cctx)
    assert out.found and out.source == "cold" and out.type == "vm"
    missing = fetch_resource(FetchResourceIn(resource_id="vm-nope", at=t), ctx=cctx)
    assert not missing.found and missing.source == "cold"


def test_history_and_diff_bodies(ctx):
    cctx, snap = ctx
    rid = _a_vm(snap)
    h = resource_history(ResourceHistoryIn(resource_id=rid), ctx=cctx)
    assert h.total_count >= 1 and h.events[0].op == "upsert"
    t0 = snap[0].event_time.replace(tzinfo=UTC) - timedelta(seconds=1)
    t1 = snap[-1].event_time.replace(tzinfo=UTC) + timedelta(seconds=1)
    d = world_diff(WorldDiffIn(from_t=t0, to_t=t1), ctx=cctx)
    assert d.counts["created"] >= RESOURCES // 2 and d.refs


def test_traversal_bodies_and_composite(ctx):
    cctx, snap = ctx
    rid = _a_vm(snap)
    live = blast_radius(BlastRadiusIn(resource_id=rid, depth=2), ctx=cctx)
    assert live.source == "hot"
    t = snap[-1].event_time.replace(tzinfo=UTC) + timedelta(seconds=1)
    cold = blast_radius(BlastRadiusIn(resource_id=rid, depth=2, at=t), ctx=cctx)
    assert cold.source == "composite"
    assert {r.id for r in live.refs} == {r.id for r in cold.refs}
    if live.refs:
        p = dependency_path(DependencyPathIn(from_id=live.refs[0].id, to_id=rid), ctx=cctx)
        assert p.found and p.path[-1] == rid


def test_mcp_server_call_path_in_process(ctx, monkeypatch: pytest.MonkeyPatch):
    from resgraph.mcp import server as mcp_server

    cctx, snap = ctx
    rid = _a_vm(snap)
    # the adapter closes its context's session after the call — hand it
    # a private one so the module-scoped session survives
    own = get_driver()
    monkeypatch.setattr(
        mcp_server,
        "_query_context",
        lambda: QueryContext(session=own.session(), catalog=cctx.query.catalog),
    )
    s = mcp_server.build_server()

    async def call():
        out = await s.call_tool("fetch_resource", {"resource_id": rid})
        prompt = await s.get_prompt("incident-impact")
        return out, prompt

    out, prompt = asyncio.run(call())
    assert not out.is_error and out.structured_content["found"] is True
    assert "blast_radius" in prompt.messages[0].content.text


def test_http_tool_route_executes_the_canonical_body(ctx):
    from fastapi.testclient import TestClient

    from resgraph.api.app import app

    cctx, snap = ctx
    rid = _a_vm(snap)
    client = TestClient(app)
    r = client.post("/tools/fetch_resource", json={"resource_id": rid})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True and body["id"] == rid


def test_server_query_context_factories_reach_both_stores(ctx, monkeypatch: pytest.MonkeyPatch):
    from resgraph.mcp import server as mcp_server

    cctx, _ = ctx
    monkeypatch.setattr(mcp_server, "_driver", None)
    monkeypatch.setattr(mcp_server, "_catalog", None)
    monkeypatch.setenv(
        "RESGRAPH_COLD_DIR", str(cctx.query.catalog.properties["warehouse"]).removeprefix("file://")
    )
    qctx = mcp_server._query_context()
    session = qctx.require("hot")
    assert session.run("RETURN 1 AS ok").single()["ok"] == 1
    session.close()
    assert qctx.require("cold") is not None
