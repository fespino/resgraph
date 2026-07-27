# resgraph SPEC

Decision log + phase contracts. Locked decisions carry D-NN ids; changing
one requires a new decision superseding it, not an edit.

## Phase 0 — foundations

### D1 — Graph hot store: Memgraph (Community)

| Criterion | Memgraph | Neo4j Community |
|---|---|---|
| Runtime | C++, in-memory-first; low footprint | JVM; heavier baseline |
| Laptop perf (our fleet) | fast start, low RAM floor | slower start, GC tuning |
| Query language | Cypher (Bolt protocol) | Cypher (Bolt) — same skills |
| Algorithms | MAGE library | APOC/GDS (richer, but GDS licensing) |
| Tooling/ecosystem | Lab, smaller community | Browser, huge docs, name recognition |
| License | Community: free, source-available | GPLv3, no clustering |

**Decision:** Memgraph. Rationale: performance-per-watt on a laptop fleet
(performance is a budget), instant startup for test cycles, and
Cypher/Bolt compatibility means the skill and most queries transfer to
Neo4j unchanged.
**Rejected:** Neo4j — better name recognition, heavier local footprint.
**Reversal condition:** if later on we hit MAGE/tooling gaps that cost
more than a day, or traversal benchmarks disqualify Memgraph, switch —
the Bolt driver and Cypher carry over; only Compose + index DDL change.

### D2 — Update message schema (verbatim; the generator/ingest contract)

```json
{
    "schema_version": 1,
    "sequence": 184467,
    "event_time": "2026-07-17T14:03:22.512Z",
    "op": "upsert",
    "resource_type": "vm",
    "resource_id": "vm-a1b2c3",
    "attrs": {"zone": "z1", "cpu": 4, "state": "running"},
    "relationships": [
        {"type": "runs_on", "target_id": "host-9f8e"},
        {"type": "member_of", "target_id": "asg-web"}
    ]
}
```

Semantics (normative):
- `sequence`: uint64, **globally monotonic from the generator**. Ordering
  is guaranteed per `resource_id` only; consumers MUST NOT assume global
  order after transport.
- `op` ∈ {`upsert`, `delete`}. `delete` carries empty `attrs` and
  `relationships`.
- `relationships` are **owned by the source resource** and replace-on-
  upsert (the message is a full statement of the resource's outbound
  edges, not a diff). Referential integrity at emit time is NOT
  guaranteed at apply time (targets may arrive later or be deleted);
  consumers handle dangling edges.
- `event_time` is generator world-time; processing time is the
  consumer's problem.
- `resource_type` ∈ {vm, host, db, lb, sg, container, asg} (Phase 1 set;
  additive growth only).
- Parsing is **strict**: consumers MUST reject messages carrying unknown
  fields. New fields arrive only via a `schema_version` bump, never by
  producers emitting ahead of the contract — so an unknown field is a
  producer bug, not forward compatibility.

**Rejected:** diff-based relationship updates (add/remove edge ops) —
cheaper messages, but replace-on-upsert makes idempotent reapplication
trivial and matches how real inventory APIs (cloud asset feeds) behave.
**Reversal condition:** if future benchmarks show edge-replacement
dominating write cost at fleet scale, introduce `relationships_diff` as
schema_version 2, additive.

### D3 — Idempotency: per-resource applied-sequence watermark

Each resource node in the hot store carries `applied_seq` (uint64). The
ingest applies a message iff `msg.sequence > node.applied_seq`, in the
same transaction as the write. Deletes write a **tombstone**
(`deleted=true, deleted_seq`) rather than removing the node; a later
upsert with higher sequence revives it (out-of-order safety). Tombstone
GC is a future concern (cold store holds history).

Consequences: at-least-once delivery is safe (replays are no-ops);
out-of-order within a resource is safe (stale messages skipped);
cross-resource ordering is explicitly not promised (per D2).
**Rejected:** global dedup table keyed by (resource_id, sequence) —
extra lookup per message and it grows forever; the watermark rides on
the node we're touching anyway.
**Reversal condition:** if a consumer ever needs exactly-once *side
effects* beyond the store (e.g., notifications), add an outbox — do not
weaken the watermark.

### D4 — Performance budgets (provisional until ingest baselines exist)

| Budget | Target | Measured (fill at Ch 3/2) |
|---|---|---|
| Ingest throughput, single consumer | ≥ 20k updates/s | — |
| Traversal p95, depth ≤ 3, 100k-resource world | < 50 ms | — |
| Ingest memory ceiling | < 512 MB RSS | — |
| World generator emit rate | ≥ 100k msg/s | — |

Provisional targets exist to be *validated, then enforced* (as CI
gates, once measured). A budget without a measurement is a wish; a measurement
without a budget is trivia.

## Phase contracts
- The generator MUST emit D2 messages exactly and expose `--seed`
  for reproducibility.
- The hot-store ingest MUST implement D3 as stated.
- Any increment touching these contracts cites the D-number in its PR.


