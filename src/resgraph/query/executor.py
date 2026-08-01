"""Plan execution over both stores (D16).

Nothing here decides placement — that happened at plan time. This module
runs the steps: hot Cypher, cold DuckDB reconstruction, ephemeral BFS,
and the residual filter that the plan already flagged.
"""

from collections import deque
from dataclasses import dataclass
from typing import Any

from resgraph.cold import queries as cold_queries
from resgraph.graph import queries as hot_queries
from resgraph.graph.ingest import SYSTEM_PROPS

from .dsl import Predicate
from .planner import Plan, _cypher_where, _duckdb_where, bind_params


@dataclass
class QueryContext:
    session: Any = None
    catalog: Any = None

    def require(self, store: str):
        handle = self.session if store == "hot" else self.catalog
        if handle is None:
            raise RuntimeError(f"query needs the {store} store but none is configured")
        return handle


def _matches(row: dict, p: Predicate) -> bool:
    if p.field.startswith("attrs."):
        value = row.get("attrs", {}).get(p.field.removeprefix("attrs."))
    else:
        value = row.get(p.field)
    if value is None:
        return False
    if p.op in ("<", "<=", ">", ">=") and not isinstance(value, (int, float)):
        return False
    try:
        return {
            "=": lambda: value == p.value,
            "!=": lambda: value != p.value,
            "<": lambda: value < p.value,
            "<=": lambda: value <= p.value,
            ">": lambda: value > p.value,
            ">=": lambda: value >= p.value,
        }[p.op]()
    except TypeError:
        return False


def _residual_filter(rows: list[dict], residual: list[Predicate]) -> list[dict]:
    return [r for r in rows if all(_matches(r, p) for p in residual)]


def _blast_bfs(rows: list[dict], root: str, depth: int) -> set[str]:
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


def execute_plan(plan: Plan, ctx: QueryContext) -> list[dict]:
    q = plan.query
    claimable = [p for p in q.predicates if p not in plan.residual]

    if q.at is None:
        session = ctx.require("hot")
        affected = hot_queries.blast_radius(
            session,
            q.root,
            depth=q.depth,
            extra_where=_cypher_where(claimable),
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
            where=_duckdb_where(claimable) or None,
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

    state = cold_queries.state_at(
        catalog,
        q.at,
        where=_duckdb_where(claimable) or None,
        params=bind_params(claimable),
        annotate=True,
    )
    affected_ids = _blast_bfs(state, q.root, q.depth)
    rows = [
        {"id": r["resource_id"], "type": r["resource_type"], "attrs": r["attrs"]}
        for r in state
        if r["resource_id"] in affected_ids and r["matched"]
    ]
    rows = _residual_filter(rows, plan.residual)
    return [{"id": r["id"], "type": r["type"]} for r in sorted(rows, key=lambda r: r["id"])]
