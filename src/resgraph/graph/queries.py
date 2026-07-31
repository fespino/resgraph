"""The traversal queries that justify a graph store's existence (D8).

Direction convention (D8): edges point dependent -> dependency, so the
blast radius of X is everything with a directed path TO X.
"""

from pydantic import BaseModel

from resgraph.gen.world import TOPOLOGY

from .client import cypher

DEP_EDGES = ":RUNS_ON | :ATTACHED_TO | :ROUTES_TO | :MEMBER_OF"

MAX_DEPTH = 6


class Affected(BaseModel):
    id: str
    type: str
    phantom: bool | None = None


class PathResult(BaseModel):
    path: list[str]
    rels: list[str]


def _check_depth(depth) -> int:
    # Cypher can't parametrize variable-length bounds, so depth lands in
    # the query string — int() + range check is the injection guard, and
    # the cap is a tool-level bound: callers must not be able to ask an
    # unbounded question.
    depth = int(depth)
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be 1..{MAX_DEPTH}")
    return depth


def blast_radius(
    session, resource_id: str, depth: int = 3, include_deleted: bool = False
) -> list[Affected]:
    """Everything affected if resource_id dies. BFS expansion: one
    shortest path per reachable node — variable-length expansion would
    enumerate paths (exponential on hubs; measured in BENCHMARKS.md)."""
    depth = _check_depth(depth)
    where = "" if include_deleted else "WHERE NOT coalesce(dep.deleted, false)"
    rows = cypher(
        session,
        f"""
        MATCH (x {{id: $id}})<-[{DEP_EDGES} *BFS 1..{depth}]-(dep)
        {where}
        RETURN DISTINCT dep.id AS id, labels(dep)[0] AS type, dep.phantom AS phantom
        """,
        id=resource_id,
    )
    return sorted((Affected(**r) for r in rows), key=lambda a: a.id)


def dependency_path(session, from_id: str, to_id: str) -> PathResult | None:
    """Why does A depend on B — one shortest dependency path."""
    rows = cypher(
        session,
        f"""
        MATCH p = (a {{id: $a}})-[{DEP_EDGES} *BFS 1..{MAX_DEPTH}]->(b {{id: $b}})
        RETURN [n IN nodes(p) | n.id] AS path,
               [r IN relationships(p) | type(r)] AS rels
        LIMIT 1
        """,
        a=from_id,
        b=to_id,
    )
    return PathResult(**rows[0]) if rows else None


def _required_edges() -> list[tuple[str, str, str]]:
    # Generated from the D5 table (cardinality exactly-1), not hand-written
    # — the topology table is code now; D5 pays rent.
    return sorted(
        (src.value, rel_type.upper(), targets[0].value)
        for (src, rel_type), (targets, lo, hi) in TOPOLOGY.items()
        if lo == 1 and hi == 1
    )


def orphans(session) -> list[dict]:
    """Live resources whose required (exactly-1) dependency is deleted or
    phantom."""
    out: list[dict] = []
    for src_label, rel_type, dst_label in _required_edges():
        out.extend(
            cypher(
                session,
                f"""
                MATCH (c:{src_label})-[:{rel_type}]->(d:{dst_label})
                WHERE NOT coalesce(c.deleted, false)
                  AND (coalesce(d.deleted, false) OR coalesce(d.phantom, false))
                RETURN c.id AS id, d.id AS missing_dependency
                """,
            )
        )
    return sorted(out, key=lambda r: r["id"])


def stats(session) -> dict:
    nodes = cypher(
        session,
        """
        MATCH (n)
        RETURN labels(n)[0] AS type, count(n) AS total,
               sum(CASE WHEN coalesce(n.deleted, false) THEN 1 ELSE 0 END) AS deleted,
               sum(CASE WHEN coalesce(n.phantom, false) THEN 1 ELSE 0 END) AS phantom
        ORDER BY type
        """,
    )
    edges = cypher(
        session,
        "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS total ORDER BY type",
    )
    return {"nodes": nodes, "edges": edges}
