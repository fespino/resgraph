"""Plan execution over both stores (D16).

Nothing here decides placement — that happened at plan time. This module
runs the steps: hot Cypher, cold DuckDB reconstruction, ephemeral BFS,
and the residual filter that the plan already flagged.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, assert_never

from resgraph.cold import queries as cold_queries
from resgraph.graph import queries as hot_queries
from resgraph.graph.ingest import SYSTEM_PROPS

from .dsl import Predicate
from .planner import (
    Plan,
    bind_params,
    composite_projection,
    cypher_where,
    duckdb_where,
    where_cols,
)


@dataclass
class QueryContext:
    """Store handles, created on first use — a plan that never executes
    (explain) must never open a connection."""

    session: Any = None
    catalog: Any = None
    session_factory: Callable[[], Any] | None = None
    catalog_factory: Callable[[], Any] | None = None

    def require(self, store: str):
        if store == "hot":
            if self.session is None and self.session_factory is not None:
                self.session = self.session_factory()
            if self.session is None:
                raise RuntimeError("query needs the hot store but none is configured")
            return self.session
        if self.catalog is None and self.catalog_factory is not None:
            self.catalog = self.catalog_factory()
        if self.catalog is None:
            raise RuntimeError("query needs the cold store but none is configured")
        return self.catalog


def _matches(row: dict[str, Any], p: Predicate) -> bool:
    if p.field.startswith("attrs."):
        value = row.get("attrs", {}).get(p.field.removeprefix("attrs."))
    else:
        value = row.get(p.field)
    if value is None:
        return False
    match p.op:
        case "=":
            return value == p.value
        case "!=":
            return value != p.value
        case "<" | "<=" | ">" | ">=":
            # both sides numeric or the comparison is meaningless — the
            # old TypeError catch, made statically impossible instead
            if not isinstance(value, (int, float)) or not isinstance(p.value, (int, float)):
                return False
            if p.op == "<":
                return value < p.value
            if p.op == "<=":
                return value <= p.value
            if p.op == ">":
                return value > p.value
            return value >= p.value
        case _:
            assert_never(p.op)


def _residual_filter(rows: list[dict[str, Any]], residual: list[Predicate]) -> list[dict[str, Any]]:
    return [r for r in rows if all(_matches(r, p) for p in residual)]


def _blast_bfs(rows: list[dict[str, Any]], root: str, depth: int) -> set[str]:
    """Blast radius over reconstructed state: BFS along reversed dependency
    edges (D8 direction — dependents have a path TO the root)."""
    dependents: dict[str, list[str]] = {}
    for r in rows:
        for rel in r["relationships"]:
            dependents.setdefault(rel["target_id"], []).append(r["resource_id"])
    seen = {root}
    frontier = deque([(root, 0)])
    while frontier:
        node, d = frontier.popleft()
        if d == depth:
            continue
        for dep in dependents.get(node, ()):
            if dep not in seen:
                seen.add(dep)
                frontier.append((dep, d + 1))
    seen.discard(root)
    return seen


def execute_plan(plan: Plan, ctx: QueryContext) -> list[dict[str, Any]]:
    q = plan.query
    claimable = [p for p in q.predicates if p not in plan.residual]

    if q.at is None:
        if q.root is None:
            raise ValueError("live plan without a root (D16: only blast_radius runs live)")
        session = ctx.require("hot")
        affected = hot_queries.blast_radius(
            session,
            q.root,
            depth=q.depth,
            extra_where=cypher_where(claimable),
            params=bind_params(claimable),
            with_attrs=bool(plan.residual),
        )
        rows = [
            {
                "id": a.id,
                "type": a.type,
                "attrs": {k: v for k, v in (a.attrs or {}).items() if k not in SYSTEM_PROPS},
            }
            for a in affected
        ]
        rows = _residual_filter(rows, plan.residual)
        return [{"id": r["id"], "type": r["type"]} for r in rows]

    catalog = ctx.require("cold")
    if q.kind == "world":
        state = cold_queries.state_at(
            catalog,
            q.at,
            where=duckdb_where(claimable) or None,
            params=bind_params(claimable),
        )
        rows = [
            {
                "id": r["resource_id"],
                "type": r["resource_type"],
                "attrs": r["attrs"],
                "relationships": r["relationships"],
            }
            for r in state
        ]
        return _residual_filter(rows, plan.residual)

    if q.root is None:
        raise ValueError("composite plan without a root (D16)")
    state = cold_queries.state_at(
        catalog,
        q.at,
        where=duckdb_where(claimable) or None,
        params=bind_params(claimable),
        annotate=True,
        projection=composite_projection(bool(plan.residual)),
        where_cols=where_cols(claimable),
    )
    affected_ids = _blast_bfs(state, q.root, q.depth)
    rows = [
        {"id": r["resource_id"], "type": r["resource_type"], "attrs": r.get("attrs", {})}
        for r in state
        if r["resource_id"] in affected_ids and r["matched"]
    ]
    rows = _residual_filter(rows, plan.residual)
    return [{"id": r["id"], "type": r["type"]} for r in sorted(rows, key=lambda r: r["id"])]
