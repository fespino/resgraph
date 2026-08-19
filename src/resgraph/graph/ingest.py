"""Streaming-ingest apply — the idempotent, transactional write path.

Every message goes through ``apply_message``, which enforces a
per-resource watermark and the write in a SINGLE transaction:

    apply iff  msg.sequence > node.applied_seq

The consequences the property tests pin (test_ingest_properties.py):

- **Replay is a no-op.** Re-applying any message returns ``False`` and
  leaves the node untouched — at-least-once delivery is safe.
- **Out-of-order within a resource is safe.** A stale (lower-sequence)
  message is skipped, whatever order it arrives in.
- **Delete tombstones, never removes.** A ``delete`` sets
  ``deleted=true, deleted_seq`` and drops the resource's outbound edges;
  the node stays so a higher-sequence upsert can revive it. Reclaiming
  tombstones is a later concern — the cold store keeps the history.
- **Attrs and relationships are a full statement.** An upsert *replaces*
  the resource's attrs and outbound edges; the message is not a diff.
  This is what makes the final state a pure function of the
  highest-sequence message, which is what makes ordering not matter.
- **Dangling targets become phantom placeholders.** An edge to an unknown
  target MERGEs a ``phantom=true`` node; that target's own upsert later
  clears the flag.

CONVERGENCE is the property all of the above serve: applying a resource's
messages in ANY order yields the same final node state — the state
implied by the single highest-sequence message. The watermark exists to
buy exactly that; everything else is a corollary.

TODO(#31): all of this assumes a SINGLE producer assigns each resource's
sequences monotonically (true for the generator). With multiple writers
per resource the watermark can't arbitrate — sequence assignment needs
epochs/fencing. Second iteration, tracked in issue #31.

Decisions: D3, D10 (SPEC.md).
"""

from typing import Any, assert_never

from neo4j import ManagedTransaction, Session

from resgraph import obs
from resgraph.schema import RESERVED_ATTR_KEYS, Op, UpdateMessage

from .client import lit
from .schema import label_for

# Outbound edge types the ingest owns; an upsert replaces exactly these —
# the message states the resource's full edge set, not a diff.
DEP_REL_TYPES = ("RUNS_ON", "ATTACHED_TO", "ROUTES_TO", "MEMBER_OF")

# Node properties the store manages; everything else on a node is a
# user-supplied attr. The schema rejects attrs under these keys at parse
# time, so the store never has to disambiguate — one constant, shared.
SYSTEM_PROPS = RESERVED_ATTR_KEYS


def apply_message(session: Session, msg: UpdateMessage) -> bool:
    """Apply one message idempotently. Returns ``True`` if it was applied,
    ``False`` if the watermark skipped it as stale/replayed.

    The watermark check and the write happen in one transaction, so the
    result is the same under replay and under any arrival order.
    """
    label = label_for(msg.resource_id)
    return session.execute_write(_apply, msg, label)


def _apply(tx: ManagedTransaction, msg: UpdateMessage, label: str) -> bool:
    rec = tx.run(
        lit(f"MATCH (n:{label} {{id: $id}}) RETURN n.applied_seq AS s"), id=msg.resource_id
    ).single()
    current = rec["s"] if rec and rec["s"] is not None else -1
    if msg.sequence <= current:  # strict watermark: stale or replayed
        return False
    match msg.op:
        case Op.UPSERT:
            _write_upsert(tx, msg, label)
        case Op.DELETE:
            _write_tombstone(tx, msg, label)
        case _:
            assert_never(msg.op)
    return True


def _write_upsert(tx: ManagedTransaction, msg: UpdateMessage, label: str) -> None:
    # Replace the property bag wholesale (the message is a full statement),
    # then re-assert the system fields so they win regardless of attr keys.
    tx.run(
        lit(f"""
        MERGE (n:{label} {{id: $id}})
        SET n = $attrs
        SET n.id = $id, n.applied_seq = $seq,
            n.deleted = false, n.deleted_seq = null, n.phantom = false
        """),
        id=msg.resource_id,
        seq=msg.sequence,
        attrs=dict(msg.attrs),
    ).consume()
    # Relationships replace-on-upsert: clear the owned edge types, re-create.
    tx.run(
        lit(f"MATCH (:{label} {{id: $id}})-[r]->() WHERE type(r) IN $deps DELETE r"),
        id=msg.resource_id,
        deps=list(DEP_REL_TYPES),
    ).consume()
    phantoms = 0
    for rel in msg.relationships:
        rel_type = rel.type.upper()
        # unreachable given the schema, but the type lands in the query string
        if rel_type not in DEP_REL_TYPES:
            raise ValueError(f"unknown relationship type: {rel.type!r}")
        summary = tx.run(
            lit(f"""
            MATCH (s:{label} {{id: $sid}})
            MERGE (t:{label_for(rel.target_id)} {{id: $tid}})
              ON CREATE SET t.phantom = true
            MERGE (s)-[:{rel_type}]->(t)
            """),
            sid=msg.resource_id,
            tid=rel.target_id,
        ).consume()
        # sources are MATCHed, so anything this query created is a phantom
        phantoms += summary.counters.nodes_created
    if phantoms:
        obs.PHANTOMS_CREATED.add(phantoms)


def _write_tombstone(tx: ManagedTransaction, msg: UpdateMessage, label: str) -> None:
    # A tombstone carries no payload — clear props and drop outbound edges —
    # which is what makes a highest-sequence delete order-independent. The
    # node stays so a later higher-sequence upsert can revive it.
    tx.run(
        lit(f"""
        MERGE (n:{label} {{id: $id}})
        SET n = {{}}
        SET n.id = $id, n.applied_seq = $seq,
            n.deleted = true, n.deleted_seq = $seq, n.phantom = false
        """),
        id=msg.resource_id,
        seq=msg.sequence,
    ).consume()
    tx.run(
        lit(f"MATCH (:{label} {{id: $id}})-[r]->() WHERE type(r) IN $deps DELETE r"),
        id=msg.resource_id,
        deps=list(DEP_REL_TYPES),
    ).consume()


def apply_batch(session: Session, msgs: list[UpdateMessage]) -> tuple[int, int]:
    """Apply a batch of messages in ONE transaction. Returns
    ``(applied, skipped)``. Semantically identical to calling
    ``apply_message`` per message — pinned by a property test — but
    ~40x fewer round trips: the per-message path costs 6+ sequential
    round trips (BEGIN, watermark read, writes, COMMIT), which the
    profile showed as ~80% driver receive path — over a third of it
    raw socket wait (BENCHMARKS.md).

    Counter note: a message superseded by a higher-sequence sibling in
    the SAME batch counts as skipped — the Python pre-pass hands down
    the same verdict the store watermark would, one round trip earlier.

    Crash safety: the batch commits or rolls back atomically. A rollback
    redelivers the whole batch, and reapplication converges — same
    argument as single-message replay, at batch granularity.
    """
    if not msgs:
        return (0, 0)
    return session.execute_write(_apply_batch_tx, list(msgs))


def _apply_batch_tx(tx: ManagedTransaction, msgs: list[UpdateMessage]) -> tuple[int, int]:
    # Highest sequence per resource wins — the watermark's verdict for
    # intra-batch siblings, computed in Python (convergence makes the
    # outcome identical to applying them one by one).
    best: dict[str, UpdateMessage] = {}
    for m in msgs:
        cur = best.get(m.resource_id)
        if cur is None or m.sequence > cur.sequence:
            best[m.resource_id] = m
    skipped = len(msgs) - len(best)

    # Watermark reads, one statement per label.
    by_label: dict[str, list[UpdateMessage]] = {}
    for m in best.values():
        by_label.setdefault(label_for(m.resource_id), []).append(m)
    current: dict[str, int] = {}
    for label, group in sorted(by_label.items()):
        rows = tx.run(
            lit(f"UNWIND $ids AS id MATCH (n:{label} {{id: id}}) RETURN id, n.applied_seq AS s"),
            ids=[m.resource_id for m in group],
        )
        for rec in rows:
            current[rec["id"]] = rec["s"] if rec["s"] is not None else -1

    apply = [m for m in best.values() if m.sequence > current.get(m.resource_id, -1)]
    skipped += len(best) - len(apply)
    if not apply:
        return (0, skipped)

    # Node writes, per label: upserts replace the property bag, deletes
    # write tombstones — same statements as the single-message path,
    # UNWIND-batched.
    upserts: dict[str, list[dict[str, Any]]] = {}
    tombstones: dict[str, list[dict[str, Any]]] = {}
    for m in apply:
        label = label_for(m.resource_id)
        if m.op is Op.UPSERT:
            upserts.setdefault(label, []).append(
                {"id": m.resource_id, "seq": m.sequence, "attrs": dict(m.attrs)}
            )
        else:
            tombstones.setdefault(label, []).append({"id": m.resource_id, "seq": m.sequence})
    for label, rows in sorted(upserts.items()):
        tx.run(
            lit(f"""
            UNWIND $rows AS r
            MERGE (n:{label} {{id: r.id}})
            SET n = r.attrs
            SET n.id = r.id, n.applied_seq = r.seq,
                n.deleted = false, n.deleted_seq = null, n.phantom = false
            """),
            rows=rows,
        ).consume()
    for label, rows in sorted(tombstones.items()):
        tx.run(
            lit(f"""
            UNWIND $rows AS r
            MERGE (n:{label} {{id: r.id}})
            SET n = {{}}
            SET n.id = r.id, n.applied_seq = r.seq,
                n.deleted = true, n.deleted_seq = r.seq, n.phantom = false
            """),
            rows=rows,
        ).consume()

    # Edge clears for every applied resource, per label — then creates,
    # grouped by (source label, type, target label) so both ends hit
    # indexes (the loader's trick). Creates run last so clears can't eat
    # them and phantom targets MERGE correctly.
    clears: dict[str, list[str]] = {}
    for m in apply:
        clears.setdefault(label_for(m.resource_id), []).append(m.resource_id)
    for label, ids in sorted(clears.items()):
        tx.run(
            lit(f"""
            UNWIND $ids AS id
            MATCH (:{label} {{id: id}})-[r]->() WHERE type(r) IN $deps DELETE r
            """),
            ids=ids,
            deps=list(DEP_REL_TYPES),
        ).consume()
    edges: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for m in apply:
        if m.op is not Op.UPSERT:
            continue
        src_label = label_for(m.resource_id)
        for rel in m.relationships:
            rel_type = rel.type.upper()
            # unreachable given the schema, but the type lands in the query string
            if rel_type not in DEP_REL_TYPES:
                raise ValueError(f"unknown relationship type: {rel.type!r}")
            key = (src_label, rel_type, label_for(rel.target_id))
            edges.setdefault(key, []).append({"src": m.resource_id, "dst": rel.target_id})
    phantoms = 0
    for (src_label, rel_type, dst_label), rows in sorted(edges.items()):
        summary = tx.run(
            lit(f"""
            UNWIND $rows AS e
            MATCH (s:{src_label} {{id: e.src}})
            MERGE (t:{dst_label} {{id: e.dst}})
              ON CREATE SET t.phantom = true
            MERGE (s)-[:{rel_type}]->(t)
            """),
            rows=rows,
        ).consume()
        # sources are MATCHed, so anything this query created is a phantom
        phantoms += summary.counters.nodes_created
    if phantoms:
        obs.PHANTOMS_CREATED.add(phantoms)

    return (len(apply), skipped)


def read_node(session: Session, resource_id: str) -> dict[str, Any] | None:
    """Normalized node state for assertions and the CLI, or ``None`` if
    the node is absent.

    Keys: ``id``, ``applied_seq``, ``deleted``, ``deleted_seq``,
    ``phantom``, ``attrs`` (user attrs only, system props stripped), and
    ``rels`` (sorted ``(TYPE, target_id)`` tuples for outbound edges).
    """
    label = label_for(resource_id)
    rec = session.run(
        lit(f"""
        MATCH (n:{label} {{id: $id}})
        OPTIONAL MATCH (n)-[r]->(t)
        RETURN properties(n) AS props,
               collect({{type: type(r), target: t.id}}) AS rels
        """),
        id=resource_id,
    ).single()
    if rec is None or rec["props"] is None:
        return None
    props = dict(rec["props"])
    attrs = {k: v for k, v in props.items() if k not in SYSTEM_PROPS}
    rels = sorted((rr["type"], rr["target"]) for rr in rec["rels"] if rr["type"] is not None)
    return {
        "id": props.get("id"),
        "applied_seq": props.get("applied_seq"),
        "deleted": bool(props.get("deleted", False)),
        "deleted_seq": props.get("deleted_seq"),
        "phantom": bool(props.get("phantom", False)),
        "attrs": attrs,
        "rels": rels,
    }
