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


def init_schema(session) -> None:
    for stmt in DDL:
        # already-exists errors are the idempotency contract
        with contextlib.suppress(ClientError):
            session.run(stmt).consume()


def node_count(session) -> int:
    return session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
