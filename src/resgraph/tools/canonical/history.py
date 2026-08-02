"""resource_history and world_diff — the cold store as a tool."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from resgraph.cold import queries as cold_queries
from resgraph.tools.budgets import ResourceRef, paginate, paginate_refs
from resgraph.tools.context import CallerContext


class ResourceHistoryIn(BaseModel):
    resource_id: str
    offset: int = 0


class HistoryEvent(BaseModel):
    sequence: int
    event_time: str
    op: str
    attrs: dict[str, Any]
    relationships: list[dict[str, str]]


class _EventPage(BaseModel):
    items: list[HistoryEvent]


class ResourceHistoryOut(BaseModel):
    resource_id: str
    events: list[HistoryEvent]
    total_count: int
    truncated: bool
    pagination_hint: str | None
    fetched_at: datetime
    source: Literal["cold"]


def resource_history(args: ResourceHistoryIn, *, ctx: CallerContext) -> ResourceHistoryOut:
    """Every recorded change to one resource, oldest first."""
    catalog = ctx.query.require("cold")
    rows = cold_queries.history(catalog, args.resource_id, limit=10_000)
    events = [HistoryEvent.model_validate(r) for r in rows]
    page, truncated, hint, total = paginate(events, args.offset, "resource_history", _EventPage)
    return ResourceHistoryOut(
        resource_id=args.resource_id,
        events=page,
        total_count=total,
        truncated=truncated,
        pagination_hint=hint,
        fetched_at=datetime.now(UTC),
        source="cold",
    )


class WorldDiffIn(BaseModel):
    from_t: datetime
    to_t: datetime
    offset: int = 0


class WorldDiffOut(BaseModel):
    refs: list[ResourceRef]
    counts: dict[str, int]
    total_count: int
    truncated: bool
    pagination_hint: str | None
    fetched_at: datetime
    source: Literal["cold"]


def world_diff(args: WorldDiffIn, *, ctx: CallerContext) -> WorldDiffOut:
    """What changed between two moments — refs bucketed created/deleted/
    changed; fetch_resource for detail on the ones that matter."""
    catalog = ctx.query.require("cold")
    d = cold_queries.diff(catalog, args.from_t, args.to_t)
    refs = [
        ResourceRef(id=rid, type=rid.split("-", 1)[0], one_line=bucket)
        for bucket in ("created", "deleted", "changed")
        for rid in d[bucket]
    ]
    page, truncated, hint, total = paginate_refs(refs, args.offset, "world_diff")
    return WorldDiffOut(
        refs=page,
        counts={k: len(v) for k, v in d.items()},
        total_count=total,
        truncated=truncated,
        pagination_hint=hint,
        fetched_at=datetime.now(UTC),
        source="cold",
    )
