"""Rebuild the hot store from cold history — the DR procedure (D13).

The hot store is a projection of the log; this makes that claim
operational. state_at(T) becomes synthesized full-statement upserts fed
through the snapshot loader, carrying each resource's own last-applied
sequence — so the D3 watermark survives the rebuild and a resumed
stream ingest skips everything at or below it.

Decisions: D12 (SPEC.md).
"""

from datetime import datetime
from typing import Any

from neo4j import Session
from pyiceberg.catalog import Catalog

from resgraph.graph.ingest import apply_batch
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema
from resgraph.schema import Op, UpdateMessage

from . import queries


def synthesize_upserts(state: list[dict[str, Any]], t: datetime) -> list[UpdateMessage]:
    """One full-statement upsert per alive resource, sequence preserved."""
    return [
        UpdateMessage(
            sequence=r["sequence"],
            event_time=t,
            op=Op.UPSERT,
            resource_type=r["resource_type"],
            resource_id=r["resource_id"],
            attrs=r["attrs"],
            relationships=r["relationships"],
        )
        for r in state
    ]


def rebuild(session: Session, catalog: Catalog, t: datetime | None = None) -> dict[str, Any]:
    """Load state_at(t) into an EMPTY hot store (the loader enforces
    emptiness), then restore tombstones for everything dead at t.

    Both halves carry per-resource sequences, so every watermark
    survives the rebuild — including the dead resources', without which
    a redelivered pre-t upsert would resurrect them. Resuming the
    stream consumer afterwards is safe: replayed history at or below
    each node's applied_seq is watermark-skipped."""
    if t is None:
        t = queries.latest_event_time(catalog)
        if t is None:
            raise ValueError("cold store holds no events; nothing to rebuild from")
    state = queries.state_at(catalog, t)
    init_schema(session)
    counts = load_snapshot(session, synthesize_upserts(state, t))
    deletes = [
        UpdateMessage(
            sequence=d["sequence"],
            event_time=t,
            op=Op.DELETE,
            resource_type=d["resource_type"],
            resource_id=d["resource_id"],
        )
        for d in queries.tombstones_at(catalog, t)
    ]
    applied, _ = apply_batch(session, deletes)
    max_seq = max([r["sequence"] for r in state] + [m.sequence for m in deletes], default=-1)
    return {
        **counts,
        "tombstones": applied,
        "as_of": t.isoformat(),
        "max_sequence": max_seq,
    }
