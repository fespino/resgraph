"""Bolt session factory + instrumented query helper.

The neo4j driver on purpose: Memgraph speaks Bolt, so the driver, the
sessions, and the Cypher are Neo4j skills verbatim — D1's
transferability argument made concrete.
"""

import logging
import os
import time
from typing import Any

from neo4j import Driver, GraphDatabase

log = logging.getLogger("resgraph.graph")

DEFAULT_URI = "bolt://localhost:7687"


def get_driver(uri: str | None = None) -> Driver:
    return GraphDatabase.driver(uri or os.environ.get("RESGRAPH_BOLT_URI", DEFAULT_URI))


def cypher(session, query: str, **params: Any) -> list[dict]:
    """Run a query, return dict rows, log latency (instrument from birth)."""
    t = time.monotonic()
    rows = [r.data() for r in session.run(query, **params)]
    log.debug("cypher %.1fms rows=%d :: %s", (time.monotonic() - t) * 1e3, len(rows), query[:80])
    return rows
