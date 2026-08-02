"""Bolt session factory + instrumented query helper.

The neo4j driver on purpose: Memgraph speaks Bolt, so the driver, the
sessions, and the Cypher are Neo4j skills verbatim — D1's
transferability argument made concrete.
"""

import logging
import os
import time
from typing import Any, LiteralString, cast

from neo4j import Driver, GraphDatabase, Session

log = logging.getLogger("resgraph.graph")

DEFAULT_URI = "bolt://localhost:7687"


def lit(query: str) -> LiteralString:
    """Bless a composed Cypher string for the driver's LiteralString bound.

    Every dynamic fragment interpolated upstream is an identifier
    validated against closed sets (label_for's known types, D2's
    RelType) — the D8 injection guard; values always travel as bound
    parameters."""
    return cast(LiteralString, query)


def get_driver(uri: str | None = None) -> Driver:
    return GraphDatabase.driver(uri or os.environ.get("RESGRAPH_BOLT_URI", DEFAULT_URI))


def cypher(session: Session, query: str, **params: Any) -> list[dict[str, Any]]:
    """Run a query, return dict rows, log latency (instrument from birth)."""
    t = time.monotonic()
    rows = [r.data() for r in session.run(lit(query), **params)]
    log.debug("cypher %.1fms rows=%d :: %s", (time.monotonic() - t) * 1e3, len(rows), query[:80])
    return rows
