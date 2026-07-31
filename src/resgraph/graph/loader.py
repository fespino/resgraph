"""Snapshot loader — UNWIND batches, nodes then edges (D8, D9).

Deliberately NOT the ingest: no sequence checks, no idempotent
reapplication (that is D3, a later increment). This loads a clean
snapshot into an EMPTY store and refuses otherwise.
"""

from collections import defaultdict
from collections.abc import Iterable

from resgraph.schema import UpdateMessage

from .schema import label_for, node_count

BATCH = 1000


class StoreNotEmptyError(RuntimeError):
    pass


def load_snapshot(session, messages: Iterable[UpdateMessage]) -> dict[str, int]:
    if node_count(session) != 0:
        raise StoreNotEmptyError("snapshot loader requires an empty store (it is not the ingest)")

    msgs = list(messages)

    # Pass 1 — nodes, grouped per label (indexed MERGE).
    by_type: dict[str, list[dict]] = defaultdict(list)
    for m in msgs:
        by_type[m.resource_type.value].append(
            {"id": m.resource_id, "attrs": m.attrs, "seq": m.sequence}
        )
    for label, rows in sorted(by_type.items()):
        for i in range(0, len(rows), BATCH):
            session.run(
                f"""
                UNWIND $batch AS m
                MERGE (n:{label} {{id: m.id}})
                SET n += m.attrs, n.applied_seq = m.seq,
                    n.deleted = false, n.phantom = false
                """,
                batch=rows[i : i + BATCH],
            ).consume()

    # Pass 2 — edges, grouped by (source label, rel type, target label) so
    # both endpoints hit indexes. Two passes because MERGE-on-both-ends
    # with mixed labels in one statement is where loaders go to die — and
    # it makes the phantom mechanics (D9) explicit.
    edges: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for m in msgs:
        for rel in m.relationships:
            key = (m.resource_type.value, rel.type.upper(), label_for(rel.target_id))
            edges[key].append({"src": m.resource_id, "dst": rel.target_id})
    n_edges = 0
    for (src_label, rel_type, dst_label), rows in sorted(edges.items()):
        n_edges += len(rows)
        for i in range(0, len(rows), BATCH):
            session.run(
                f"""
                UNWIND $batch AS e
                MATCH (s:{src_label} {{id: e.src}})
                MERGE (t:{dst_label} {{id: e.dst}})
                  ON CREATE SET t.phantom = true
                MERGE (s)-[:{rel_type}]->(t)
                """,
                batch=rows[i : i + BATCH],
            ).consume()

    return {"nodes": len(msgs), "edges": n_edges}
