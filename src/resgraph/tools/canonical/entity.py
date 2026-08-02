"""fetch_resource — the one polymorphic detail fetch behind every ref."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from resgraph.cold import queries as cold_queries
from resgraph.graph.ingest import read_node
from resgraph.tools.context import CallerContext


class FetchResourceIn(BaseModel):
    resource_id: str
    at: datetime | None = None


class FetchResourceOut(BaseModel):
    found: bool
    id: str
    type: str
    phantom: bool
    attrs: dict[str, Any]
    relationships: list[dict[str, str]]
    fetched_at: datetime
    source: Literal["hot", "cold"]


def fetch_resource(args: FetchResourceIn, *, ctx: CallerContext) -> FetchResourceOut:
    """Full detail for any resource type by id — live (at=None) or as it
    was at a past moment (at=T)."""
    rid = args.resource_id
    rtype = rid.split("-", 1)[0]
    if args.at is None:
        node = read_node(ctx.query.require("hot"), rid)
        if node is None or node["deleted"]:
            return _not_found(rid, rtype, "hot")
        return FetchResourceOut(
            found=True,
            id=rid,
            type=rtype,
            phantom=node["phantom"],
            attrs=node["attrs"],
            relationships=[{"type": t.lower(), "target_id": tid} for t, tid in node["rels"]],
            fetched_at=datetime.now(UTC),
            source="hot",
        )
    catalog = ctx.query.require("cold")
    rows = cold_queries.state_at(catalog, args.at, where="resource_id = $rid", params={"rid": rid})
    if not rows:
        return _not_found(rid, rtype, "cold")
    r = rows[0]
    return FetchResourceOut(
        found=True,
        id=rid,
        type=r["resource_type"],
        phantom=False,
        attrs=r["attrs"],
        relationships=r["relationships"],
        fetched_at=datetime.now(UTC),
        source="cold",
    )


def _not_found(rid: str, rtype: str, source: Literal["hot", "cold"]) -> FetchResourceOut:
    return FetchResourceOut(
        found=False,
        id=rid,
        type=rtype,
        phantom=False,
        attrs={},
        relationships=[],
        fetched_at=datetime.now(UTC),
        source=source,
    )
