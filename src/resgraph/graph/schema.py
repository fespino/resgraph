"""Idempotent DDL — per-label id index + uniqueness constraint (D8).

Nothing else until a measured query needs it: indexes are write-cost
budgets, not decorations.
"""

import contextlib

from neo4j.exceptions import ClientError

from resgraph.schema import ResourceType

DDL = [
    *[f"CREATE INDEX ON :{t.value}(id)" for t in ResourceType],
    *[f"CREATE CONSTRAINT ON (n:{t.value}) ASSERT n.id IS UNIQUE" for t in ResourceType],
]

_LABELS = {t.value for t in ResourceType}


def label_for(resource_id: str) -> str:
    """The node label, derived from the id prefix (ids are
    ``<type>-<counter>``). This is load-bearing, not cosmetic: the id
    index is per-label (``:host(id)``), so a label-less anchor match
    forces a full node scan — measured at 9.4ms vs 0.31ms indexed on a
    100k world (BENCHMARKS.md). Validating against the known type set is
    also the injection guard, since the label lands in the query string."""
    prefix = resource_id.split("-", 1)[0]
    if prefix not in _LABELS:
        raise ValueError(f"cannot derive resource type from id: {resource_id!r}")
    return prefix


def init_schema(session) -> None:
    for stmt in DDL:
        # already-exists errors are the idempotency contract
        with contextlib.suppress(ClientError):
            session.run(stmt).consume()


def node_count(session) -> int:
    return session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
