"""Through the protocol, not around it: spawn the server over stdio
with the official client and assert the surface behaves as declared —
clamps, caps, and a followable pagination hint included."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

import pytest

from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.graph.client import get_driver
from resgraph.graph.ingest import apply_batch
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema, wipe
from resgraph.schema import UpdateMessage

pytestmark = pytest.mark.integration

SEED, RESOURCES = 42, 200
HUB_DEPS = 900


def _hub_messages(start_seq: int) -> list[UpdateMessage]:
    now = datetime.now(UTC).isoformat()
    hub = UpdateMessage.model_validate(
        {
            "schema_version": 1,
            "sequence": start_seq,
            "event_time": now,
            "op": "upsert",
            "resource_type": "host",
            "resource_id": "host-hub000",
            "attrs": {"zone": "z1"},
            "relationships": [],
        }
    )
    deps = [
        UpdateMessage.model_validate(
            {
                "schema_version": 1,
                "sequence": start_seq + 1 + i,
                "event_time": now,
                "op": "upsert",
                "resource_type": "container",
                "resource_id": f"container-hub{i:04d}",
                "attrs": {"zone": "z1"},
                "relationships": [{"type": "runs_on", "target_id": "host-hub000"}],
            }
        )
        for i in range(HUB_DEPS)
    ]
    return [hub, *deps]


@pytest.fixture(scope="module")
def seeded_world():
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")
    churn = Churn(World(SEED, RESOURCES))
    snap = list(churn.snapshot())
    with driver.session() as s:
        wipe(s)
        init_schema(s)
        load_snapshot(s, snap)
        apply_batch(s, _hub_messages(start_seq=10_000_000))
    driver.close()
    yield


def test_mcp_surface_through_stdio(seeded_world, tmp_path):
    asyncio.run(_exercise_surface(tmp_path))


async def _exercise_surface(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "resgraph.mcp.server"],
        env={**os.environ, "RESGRAPH_TELEMETRY_DIR": str(tmp_path / "telemetry")},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == {
            "blast_radius",
            "dependency_path",
            "resource_history",
            "world_diff",
            "fetch_resource",
        }

        prompts = {p.name for p in (await session.list_prompts()).prompts}
        assert {"incident-impact", "change-forensics"} <= prompts

        card = await session.read_resource("resgraph://card")
        assert "will NOT do" in card.contents[0].text

        out = await session.call_tool("blast_radius", {"resource_id": "host-hub000", "depth": 50})
        assert not out.is_error
        body = out.structured_content
        assert body is not None
        assert body["depth_clamped"] is True

        assert body["truncated"] is True
        assert body["total_count"] >= HUB_DEPS
        assert len(json.dumps(body)) // 4 <= 8000
        hint = body["pagination_hint"]
        assert hint is not None and "offset=" in hint

        offset = int(hint.split("offset=")[1].split(" ")[0].rstrip("."))
        page2 = await session.call_tool(
            "blast_radius",
            {"resource_id": "host-hub000", "depth": 50, "offset": offset},
        )
        body2 = page2.structured_content
        assert body2 is not None
        ids1 = {r["id"] for r in body["refs"]}
        ids2 = {r["id"] for r in body2["refs"]}
        assert ids1.isdisjoint(ids2) and ids2

        fetched = await session.call_tool("fetch_resource", {"resource_id": "container-hub0000"})
        fbody = fetched.structured_content
        assert fbody is not None and fbody["found"] is True
        assert fbody["relationships"] == [{"type": "runs_on", "target_id": "host-hub000"}]

        path = await session.call_tool(
            "dependency_path",
            {"from_id": "container-hub0000", "to_id": "host-hub000"},
        )
        pbody = path.structured_content
        assert pbody is not None and pbody["found"] is True
        assert pbody["path"] == ["container-hub0000", "host-hub000"]
