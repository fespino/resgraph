"""The mini planner (D16): placement, lazy plans, visible residuals.

A Plan is data until .execute(ctx) — explain() serializes it without
touching a store. Placement is a table lookup: type and generator-known
attr fields are claimable by both stores; the route decides which one
runs them; everything else is a residual filter, applied in Python and
flagged in the plan.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from resgraph.gen.world import ATTR_POOLS
from resgraph.schema import ResourceType

from .dsl import Predicate

_TYPES = frozenset(t.value for t in ResourceType)

Store = Literal["hot", "cold", "python"]

KNOWN_FIELDS: frozenset[str] = frozenset({"type"}) | frozenset(
    f"attrs.{key}" for pools in ATTR_POOLS.values() for key in pools
)


@dataclass(frozen=True)
class Step:
    store: Store
    op: str
    detail: str
    pushed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "store": self.store,
            "op": self.op,
            "detail": self.detail,
            "pushed": list(self.pushed),
        }


@dataclass(frozen=True)
class Query:
    kind: Literal["blast_radius", "world"]
    root: str | None = None
    depth: int = 3
    at: datetime | None = None
    predicates: list[Predicate] = field(default_factory=list)


@dataclass(frozen=True)
class Plan:
    query: Query
    steps: list[Step]
    residual: list[Predicate]

    def explain(self) -> dict:
        return {
            "steps": [s.as_dict() for s in self.steps],
            "residual": [str(p) for p in self.residual],
        }

    def execute(self, ctx) -> list[dict]:
        from .executor import execute_plan

        return execute_plan(self, ctx)


def place(predicates: list[Predicate]) -> tuple[list[Predicate], list[Predicate]]:
    """Split predicates into (claimable, residual)."""
    claimable = [p for p in predicates if p.field in KNOWN_FIELDS]
    residual = [p for p in predicates if p.field not in KNOWN_FIELDS]
    for p in claimable:
        if p.field == "type":
            if p.op not in ("=", "!="):
                raise ValueError(f"type only supports = and !=: {p}")
            if p.value not in _TYPES:
                raise ValueError(f"unknown type {p.value!r}")
    return claimable, residual


def _cypher_where(predicates: list[Predicate]) -> str:
    clauses = []
    for i, p in enumerate(predicates):
        if p.field == "type":
            clauses.append(f"dep:{p.value}" if p.op == "=" else f"NOT dep:{p.value}")
        else:
            prop = p.field.removeprefix("attrs.")
            op = "<>" if p.op == "!=" else p.op
            clauses.append(f"dep.{prop} {op} $p{i}")
    return " AND ".join(clauses)


def _duckdb_where(predicates: list[Predicate]) -> str:
    clauses = []
    for i, p in enumerate(predicates):
        op = "<>" if p.op == "!=" else p.op
        if p.field == "type":
            clauses.append(f"resource_type {op} $p{i}")
        else:
            key = p.field.removeprefix("attrs.")
            if isinstance(p.value, bool):
                lhs = f"TRY_CAST(json_extract(attrs, '$.{key}') AS BOOLEAN)"
            elif isinstance(p.value, (int, float)):
                lhs = f"TRY_CAST(json_extract(attrs, '$.{key}') AS DOUBLE)"
            else:
                lhs = f"json_extract_string(attrs, '$.{key}')"
            clauses.append(f"{lhs} {op} $p{i}")
    return " AND ".join(clauses)


def plan(query: Query) -> Plan:
    claimable, residual = place(query.predicates)
    steps: list[Step] = []
    if query.at is None:
        if query.kind == "world":
            raise ValueError("live /world is not an endpoint; use ?at=T (D15)")
        where = _cypher_where(claimable)
        steps.append(
            Step(
                "hot",
                "cypher",
                f"blast_radius({query.root!r}, depth<={query.depth})"
                + (f" WHERE {where}" if where else ""),
                [str(p) for p in claimable],
            )
        )
    else:
        where = _duckdb_where(claimable)
        if query.kind == "blast_radius":
            # Traversal needs the full as-of topology; predicates filter the
            # affected set, so they run as a computed match column in the
            # same scan, never as a scan reduction.
            detail = f"state_at({query.at.isoformat()})"
            if where:
                detail += f", matched := ({where})"
            steps.append(Step("cold", "duckdb", detail, [str(p) for p in claimable]))
            steps.append(
                Step("python", "traverse", f"bfs from {query.root!r} depth<={query.depth}")
            )
        else:
            steps.append(
                Step(
                    "cold",
                    "duckdb",
                    f"state_at({query.at.isoformat()})"
                    + (f" WHERE {where}" if where else ""),
                    [str(p) for p in claimable],
                )
            )
    if residual:
        steps.append(
            Step(
                "python",
                "residual_filter",
                " AND ".join(str(p) for p in residual),
                [str(p) for p in residual],
            )
        )
    return Plan(query, steps, residual)


def bind_params(predicates: list[Predicate]) -> dict[str, Any]:
    return {f"p{i}": p.value for i, p in enumerate(predicates)}
