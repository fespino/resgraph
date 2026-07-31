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
"""

from resgraph.schema import RESERVED_ATTR_KEYS, Op, UpdateMessage

from .schema import label_for

# Outbound edge types the ingest owns; an upsert replaces exactly these —
# the message states the resource's full edge set, not a diff.
DEP_REL_TYPES = ("RUNS_ON", "ATTACHED_TO", "ROUTES_TO", "MEMBER_OF")

# Node properties the store manages; everything else on a node is a
# user-supplied attr. The schema rejects attrs under these keys at parse
# time, so the store never has to disambiguate — one constant, shared.
SYSTEM_PROPS = RESERVED_ATTR_KEYS


def apply_message(session, msg: UpdateMessage) -> bool:
    """Apply one message idempotently. Returns ``True`` if it was applied,
    ``False`` if the watermark skipped it as stale/replayed.

    The watermark check and the write happen in one transaction, so the
    result is the same under replay and under any arrival order.
    """
    label = label_for(msg.resource_id)
    return session.execute_write(_apply, msg, label)


def _apply(tx, msg: UpdateMessage, label: str) -> bool:
    rec = tx.run(
        f"MATCH (n:{label} {{id: $id}}) RETURN n.applied_seq AS s", id=msg.resource_id
    ).single()
    current = rec["s"] if rec and rec["s"] is not None else -1
    if msg.sequence <= current:  # strict watermark: stale or replayed
        return False
    if msg.op is Op.UPSERT:
        _write_upsert(tx, msg, label)
    else:
        _write_tombstone(tx, msg, label)
    return True


def _write_upsert(tx, msg: UpdateMessage, label: str) -> None:
    # Replace the property bag wholesale (the message is a full statement),
    # then re-assert the system fields so they win regardless of attr keys.
    tx.run(
        f"""
        MERGE (n:{label} {{id: $id}})
        SET n = $attrs
        SET n.id = $id, n.applied_seq = $seq,
            n.deleted = false, n.deleted_seq = null, n.phantom = false
        """,
        id=msg.resource_id,
        seq=msg.sequence,
        attrs=dict(msg.attrs),
    ).consume()
    # Relationships replace-on-upsert: clear the owned edge types, re-create.
    tx.run(
        f"MATCH (:{label} {{id: $id}})-[r]->() WHERE type(r) IN $deps DELETE r",
        id=msg.resource_id,
        deps=list(DEP_REL_TYPES),
    ).consume()
    for rel in msg.relationships:
        rel_type = rel.type.upper()
        # unreachable given the schema, but the type lands in the query string
        if rel_type not in DEP_REL_TYPES:
            raise ValueError(f"unknown relationship type: {rel.type!r}")
        tx.run(
            f"""
            MATCH (s:{label} {{id: $sid}})
            MERGE (t:{label_for(rel.target_id)} {{id: $tid}})
              ON CREATE SET t.phantom = true
            MERGE (s)-[:{rel_type}]->(t)
            """,
            sid=msg.resource_id,
            tid=rel.target_id,
        ).consume()


def _write_tombstone(tx, msg: UpdateMessage, label: str) -> None:
    # A tombstone carries no payload — clear props and drop outbound edges —
    # which is what makes a highest-sequence delete order-independent. The
    # node stays so a later higher-sequence upsert can revive it.
    tx.run(
        f"""
        MERGE (n:{label} {{id: $id}})
        SET n = {{}}
        SET n.id = $id, n.applied_seq = $seq,
            n.deleted = true, n.deleted_seq = $seq, n.phantom = false
        """,
        id=msg.resource_id,
        seq=msg.sequence,
    ).consume()
    tx.run(
        f"MATCH (:{label} {{id: $id}})-[r]->() WHERE type(r) IN $deps DELETE r",
        id=msg.resource_id,
        deps=list(DEP_REL_TYPES),
    ).consume()


def read_node(session, resource_id: str) -> dict | None:
    """Normalized node state for assertions and the CLI, or ``None`` if
    the node is absent.

    Keys: ``id``, ``applied_seq``, ``deleted``, ``deleted_seq``,
    ``phantom``, ``attrs`` (user attrs only, system props stripped), and
    ``rels`` (sorted ``(TYPE, target_id)`` tuples for outbound edges).
    """
    label = label_for(resource_id)
    rec = session.run(
        f"""
        MATCH (n:{label} {{id: $id}})
        OPTIONAL MATCH (n)-[r]->(t)
        RETURN properties(n) AS props,
               collect({{type: type(r), target: t.id}}) AS rels
        """,
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
