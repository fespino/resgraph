"""blast_radius and dependency_path — task-shaped traversals."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from resgraph.graph import queries as hot_queries
from resgraph.graph.queries import MAX_DEPTH
from resgraph.query.dsl import parse_filter
from resgraph.query.executor import execute_plan
from resgraph.query.planner import Query as PlannerQuery
from resgraph.query.planner import plan as make_plan
from resgraph.tools.budgets import ResourceRef, paginate_refs
from resgraph.tools.context import CallerContext


FILTER_GRAMMAR = (
    "AND-chain of comparisons, nothing else. Terms: type=vm, "
    "attrs.<key><op><value> with ops = != < <= > >=. Ordering ops need "
    "numeric values; type supports only = and !=. No OR, no parentheses. "
    "Example: type=container AND attrs.zone=z1 AND attrs.restarts>=2"
)


class BlastRadiusIn(BaseModel):
    resource_id: str
    depth: int = 3
    at: datetime | None = Field(
        default=None, description="ISO-8601 UTC; None = live graph, set = reconstructed moment"
    )
    filter: str | None = Field(default=None, description=FILTER_GRAMMAR)
    offset: int = 0


class BlastRadiusOut(BaseModel):
    refs: list[ResourceRef]
    total_count: int
    truncated: bool
    depth_clamped: bool
    pagination_hint: str | None
    fetched_at: datetime
    source: Literal["hot", "cold", "composite"]


def blast_radius(args: BlastRadiusIn, *, ctx: CallerContext) -> BlastRadiusOut:
    """Everything affected if a resource dies: dependents with a path to
    it, live (at=None) or reconstructed at a past moment (at=T)."""
    depth = min(max(args.depth, 1), MAX_DEPTH)
    preds = parse_filter(args.filter)
    p = make_plan(
        PlannerQuery(
            "blast_radius", root=args.resource_id, depth=depth, at=args.at, predicates=preds
        )
    )
    rows = execute_plan(p, ctx.query)
    refs = [ResourceRef(id=r["id"], type=r["type"], one_line=r["type"]) for r in rows]
    page, truncated, hint, total = paginate_refs(refs, args.offset, "blast_radius")
    return BlastRadiusOut(
        refs=page,
        total_count=total,
        truncated=truncated,
        depth_clamped=depth != args.depth,
        pagination_hint=hint,
        fetched_at=datetime.now(UTC),
        source="hot" if args.at is None else "composite",
    )


class DependencyPathIn(BaseModel):
    from_id: str
    to_id: str


class DependencyPathOut(BaseModel):
    found: bool
    path: list[str]
    rels: list[str]
    fetched_at: datetime
    source: Literal["hot"]


def dependency_path(args: DependencyPathIn, *, ctx: CallerContext) -> DependencyPathOut:
    """Why does A depend on B: one shortest dependency path, live state."""
    session = ctx.query.require("hot")
    result = hot_queries.dependency_path(session, args.from_id, args.to_id)
    return DependencyPathOut(
        found=result is not None,
        path=result.path if result else [],
        rels=result.rels if result else [],
        fetched_at=datetime.now(UTC),
        source="hot",
    )
