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

| Budget | Target | Measured |
|---|---|---|
| Ingest throughput, single consumer | ≥ 20k updates/s | **~12.5k** (BENCHMARKS.md; amended below) |
| Traversal p95, depth ≤ 3, 100k-resource world | < 50 ms | **0.4 ms** (BENCHMARKS.md) |
| Ingest memory ceiling | < 512 MB RSS | **82 MB peak** (BENCHMARKS.md) |
| World generator emit rate | ≥ 100k msg/s | **~36k sustained** (missed; amended below) |

Provisional targets exist to be *validated, then enforced* (as CI
gates, once measured). A budget without a measurement is a wish; a measurement
without a budget is trivia.

**D4 amendment (phase 1, by supersession):** the generator emit
budget (≥100k msg/s) was measured and missed: ~88k msg/s kernel,
~36k msg/s sustained end-to-end on an M3/8GB laptop (BENCHMARKS.md
has methods and the profile trail). Amended to **≥30k msg/s sustained
end-to-end** on laptop hardware. Reasons, recorded: pure Python with
message validation kept ON (deliberate — the generator provably emits
valid D2), and world growth during long runs raises per-message cost.
The two algorithmic bottlenecks found en route (O(world) dangling-edge
repair, O(hot-set) target picking) are fixed and documented; disabling
validation was considered and rejected. The 100k figure is retired,
not edited away.

**D4 amendment (phase 3, by supersession):** the ingest throughput
budget (≥20k updates/s, single consumer) was measured and missed:
~12.5k updates/s at 100k messages, ~10.5k sustained at 200k, on an
M3/8GB laptop running both stores (BENCHMARKS.md has the method and
both profiles). Amended to **≥10k updates/s sustained, single
consumer** on laptop hardware. Reasons, recorded: the algorithmic
bottleneck (six to seven sequential Bolt round trips per message,
~80% of wall time in the driver's receive path) was found by profile
and fixed with per-batch transactions and per-label UNWIND writes
(760 → 12.5k, 16×); the remaining cost is Memgraph write execution
itself, with message validation kept ON (same call as phase 1) and
single-consumer sequencing by the budget's own definition.
Consumer-group parallelism is the recorded scale-out lever if a
future phase needs more than the amended figure (untested —
validation is #32). The 20k figure is retired, not edited away.

## Phase 1 — the world generator

### D5 — World topology (allowed edges)

| Source | Relationship | Target | Cardinality |
|---|---|---|---|
| vm | runs_on | host | exactly 1 |
| vm | member_of | asg | 0..1 |
| vm | attached_to | sg | 1..3 |
| container | runs_on | vm | exactly 1 |
| db | runs_on | host | exactly 1 |
| db | attached_to | sg | 1..2 |
| lb | routes_to | vm \| container | 1..4 |

Default type mix for `--resources N`: hosts 5%, vms 30%, containers
40%, dbs 5%, lbs 5%, sgs 5%, asgs 10%.
**Rejected:** free-form random edges — traversals become meaningless;
blast-radius queries need real directionality.

### D6 — Determinism contract

Given identical (seed, flags, message count), the generator emits a
byte-identical stream. Consequences:
- One `random.Random(seed)` owns ALL randomness (no module-level
  random, no set/dict-ordering dependence — iterate sorted).
- `event_time` is **simulated world time**: starts at a fixed epoch
  (2026-01-01T00:00:00Z) and advances deterministically per event
  (exponential inter-arrival times drawn from the seeded RNG).
  Wall-clock never appears in messages; `--rate` throttling is
  operational only and cannot change stream content.
**Rejected:** wall-clock event_time — kills reproducible benchmarks
and byte-identical test fixtures.

### D7 — Churn model

- **Pareto skew:** 5% of resources ("hot set", chosen at seed time)
  receive 80% of updates. Real inventories are skewed; uniform churn
  is a lie that flatters caches.
- **Op mix (defaults):** attr-update 0.80, relationship-change 0.12,
  create 0.05, delete 0.03. Deletes tombstone; a deleted resource may
  be re-created (the D3 revival path gets exercised for free).
- **Burst mode:** `--burst-every 30 --burst-len 5 --burst-x 10` —
  rate-multiplier windows, deterministic in world time.
- On delete, dependents' dangling edges are repaired in world state
  (replacement target re-rolled per D5) and surface in each
  dependent's next emitted message — the world and the stream never
  disagree at emit time.
**Rejected:** letting dangling edges persist in world state — the
generator's own invariant (targets alive at emit) would be false,
and ground-truth reconciliation against world state would be
impossible.

## Phase 2 — the graph hot store

### D8 — Graph modeling

- **One label per resource type** (`:vm`, `:host`, …) — enables
  per-type indexes and readable queries. **Rejected:** generic
  `:Resource` with a `type` property — one index, but every query pays
  a filter and the planner loses selectivity.
- **Attrs flattened to node properties** (`state`, `cpu`, `zone`, …).
  **Rejected:** a single `attrs` map property — opaque to indexes and
  to per-field Cypher predicates.
- **Edges are typed, UPPERCASE** (`RUNS_ON`, `ATTACHED_TO`,
  `ROUTES_TO`, `MEMBER_OF`), direction **dependent → dependency**
  (vm RUNS_ON host). Blast radius of X = everything with a directed
  path TO X. Getting the direction convention wrong is unfixable later
  without rewriting every query — written down now.
- Per D3: every node carries `applied_seq`; tombstones are
  `deleted=true, deleted_seq` — **queries filter out deleted nodes by
  default**; the CLI exposes `--include-deleted` for forensics.
- Indexes: per-label index + uniqueness constraint on `id`. Nothing
  else until a measured query needs it — indexes are write-cost
  budgets, not decorations.

### D9 — Dangling edges: phantom nodes

When a message references a target that doesn't exist yet (D2 permits
this after transport), create the target as a **phantom**:
`(:host {id: "host-9", phantom: true})` — no attrs, no applied_seq.
A later create/upsert fills it in and clears the flag.
**Rejected:** dropping the edge — silently loses topology and makes
ingest order-sensitive, which D3 exists to prevent.
**Rejected:** buffering edges until targets arrive — unbounded memory
and a reimplementation of what the graph already is.
Phantom count is a health metric: a rising phantom rate means the
stream is delivering topology faster than existence — or the ingest
has a bug.

## Phase 3 — the streaming ingest

### D10 — Apply-time state semantics

The D3 watermark makes ordering not matter only if the final state is a
pure function of the highest-sequence message per resource. That requires
two rules D2 left unstated:

- **Attrs are a full statement.** An upsert *replaces* the node's attr
  bag, it does not merge — the message states the resource's whole attr
  set (as it already does for relationships, D2). The snapshot loader's
  `SET n += attrs` merge is fine there (a clean load never shrinks an
  attr set) but wrong for the ingest: a merge would let a stale key from
  an earlier-applied upsert survive under a different arrival order,
  breaking convergence.
- **A tombstone carries no payload.** A `delete` clears the node's attrs
  and drops its outbound edges (mirrors D2: a delete message carries
  empty attrs/relationships). If a delete instead left attrs in place,
  the surviving attrs would depend on *which* earlier upsert landed
  before the delete — order-dependent, so non-convergent. Clearing makes
  a highest-sequence delete land on one state regardless of order.
- **Attr keys must not collide with store-managed properties.** Attrs
  flatten onto the node (D8), sharing the property namespace with `id`,
  `applied_seq`, `deleted`, `deleted_seq`, `phantom`. A colliding attr
  would be silently overwritten by the store and stripped on read —
  so it's rejected at parse time as a producer bug (same posture as
  D2's strict parsing). **Rejected:** namespacing the store's props
  (`_rg_*`) — every query, index, and DDL statement pays a rename to
  protect a producer that's already violating the contract.

Consequence: applying a resource's messages in any permutation — and any
number of replays — converges on the state implied by its
highest-sequence message. Pinned by `test_ingest_properties.py`.
**Rejected:** attr-merge on upsert — cheaper writes, but forfeits
convergence, which is the whole reason the watermark exists.
**Reversal condition:** if a producer is ever allowed to send partial
attr updates (a diff, not a statement), this decision is superseded and
the watermark alone no longer guarantees convergence — a per-field
version would be needed.

### D14 — Stream consumption model (recorded retroactively)

Made during phase 3, written down during phase 4 — the gap between
doing and recording is itself a lesson; the choices were in docstrings
and BENCHMARKS.md but not in the log this repo claims to keep.

- **One consumer group per store, `XREADGROUP` → apply → `XACK`,
  ack strictly after the apply commits.** At-least-once delivery,
  absorbed by an idempotent (hot) or dedupe-at-read (cold) apply.
- **Pending-first recovery:** on start, a consumer drains its own
  unacknowledged entries before reading new ones — restart and cold
  start are one code path. **Deferred:** `XAUTOCLAIM` of a *dead
  sibling's* pending entries — meaningless while groups are
  single-consumer; lands with #32.
- **One transaction per batch; batch size is a per-sink measured
  knob** — hot sweet spot 1024 (2048 regresses), cold wants the
  largest batch buffered (8192+: Iceberg commit overhead inverts the
  curve). BENCHMARKS.md carries both sweeps.
- **SIGTERM is not handled beyond process death, deliberately:** batch
  atomicity plus redelivery makes a hard kill safe (the crash tests
  prove it); a graceful-drain handler would add code to protect
  against nothing.
**Rejected:** fan-out topology (one group per *instance*, broadcast) —
these consumers partition work; broadcast is for cache-warmers and
notifiers, a different tool.
**Reversal condition:** a consumer whose apply is NOT
idempotent-or-dedupable may not use this model — it would need the
outbox D3 already points to.

## Phase 4 — the cold store

### D11 — Cold store engine: Iceberg via pyiceberg, queried through DuckDB

Apache Iceberg tables on the local filesystem (SQLite-backed SQL
catalog), written with pyiceberg batch appends, read by scanning to
Arrow and querying in DuckDB. Zero new containers.

**Decision drivers:** a real table format buys atomic commits, schema
evolution, and snapshot isolation — the things a directory of Parquet
files reinvents badly — while pyiceberg + DuckDB keep the whole cold
path in-process at laptop scale. The skills (Iceberg semantics, Arrow,
DuckDB) transfer to any lakehouse stack.
**Rejected:** plain Parquet directories — no atomic visibility, no
manifest pruning; every consumer reimplements a catalog.
**Rejected:** Delta Lake — write path outside Spark is thinner than
pyiceberg's, and Iceberg's catalog story fits the later REST-catalog
exit better.
**Rejected:** Spark — a JVM cluster framework to write files a laptop
process can write.
**Reversal condition:** if pyiceberg's write path can't hold the D4
cold-append budget after profiling, swap the writer (e.g. append via
DuckDB's Iceberg support or a Rust writer) — the table format and
layout (D12) survive; only the writer changes.

### D12 — Cold layout: an append-only event log plus derived snapshots

Two tables:

- **`events`** — every D2 message, append-only, one row per message:
  the D2 fields flattened, `attrs` and `relationships` as JSON strings
  (arbitrary keys stay schema-stable), partitioned by `day(event_time)`.
- **`state_snapshots`** — periodic full world state (one row per
  alive resource) tagged with `as_of_time` + the max `sequence`
  included. Derived **from the events table**, never from the hot
  store: the cold store must be able to answer alone, and deriving
  from Memgraph would silently couple the two stores' bugs.

At-least-once delivery means duplicate appends are allowed; readers
dedupe on `(resource_id, sequence)` — duplicate rows are identical by
D2, so any survivor is correct. The writer stays dumb and fast.
**Rejected:** merge-on-write current-state table — destroys history,
which is the entire point of the cold half.
**Rejected:** dedupe-at-write (equality deletes) — write amplification
and thin pyiceberg support, to save readers a `DISTINCT` they can do
in one window function.
**Rejected:** teeing to Iceberg from inside the hot ingest worker
(buffered background writer, backpressure on the hot path) — couples
the stores' failure modes and progress; a second consumer group gives
each store independent position, replay, and crash recovery for free.
The trade accepted with that choice: the stream is bounded
(`maxlen~`), so a cold consumer lagging behind eviction loses history
**silently** — where the tee would have slowed the hot path instead.
Recorded limitation; gap detection (group position vs stream head) is
the mitigation if lag is ever plausible.
**Rejected:** an `ingested_at` lineage column — commit-time lineage
already lives in Iceberg snapshot metadata, and a per-row wall-clock
would make duplicate rows non-identical, breaking the cheap
DISTINCT dedupe.
**Rejected:** partitioning by `resource_id` — high cardinality means a
file explosion per partition; hidden partitioning does not save a bad
spec. (`resource_type` as a second partition dimension is deferred
until a measured query wants the pruning.)
**Rejected:** log compaction as the stream-retention strategy (retain
the latest record per key — keyspace-bounded, and a compacted log can
bootstrap subscribers forever) — Redis Streams has no native
compaction, and in this design the cold store plays that role:
`state_snapshots` is the compacted view, `events` is the full log,
and the stream stays transport.
**Reversal condition:** if the benchmark shows read-side dedupe
dominating as-of latency at target scale, add a compaction job that
rewrites deduped partitions — the read semantics stay identical.

### D13 — Time travel is event time, not commit time

`state_at(T)`: for each resource, the highest-sequence event with
`event_time <= T`; upsert rows become state, delete rows mean absent
(tombstone semantics per D10). Implementation: nearest snapshot with
`as_of_time <= T`, then replay events in `(snapshot.max_seq, T]` —
the same checkpoint-plus-log shape the hot store's bootstrap uses.

Iceberg has native time travel (query a table as of a past commit),
and it is deliberately **not** the user-facing mechanism: commit time
is when the *ingest* ran, event time is when the *world* changed, and
the two drift apart under backfill, replay, or consumer lag. Iceberg
snapshots remain what they are — physical isolation and rollback —
while `AS OF` questions are answered from the data.
**Rejected:** exposing Iceberg snapshot time travel as the as-of API —
correct only while ingestion is perfectly live; silently wrong the
first time history is replayed.
**Reversal condition:** none foreseen for the semantics; if the
checkpoint-plus-log implementation misses the D4 as-of budget, tune
snapshot cadence or add partition pruning before touching semantics.

**D12/D13 addendum (reviewed against Kreps' "The Log"):** this design
splits what Kafka unifies — the *subscribable* log (Redis stream,
ephemeral) and the *durable* log (the events table). Two consequences,
named so they stop being implicit:

- **New subscribers cannot bootstrap from the stream.** The platform's
  onboarding pattern is state-transfer-plus-tail: bootstrap from the
  cold store (`state_at`, or a full events replay for
  history-dependent consumers), then tail the stream — replayed
  overlap is absorbed by the consumer's own idempotency/dedupe.
  `rebuild` is this pattern's first instance, not a special case.
- **Reprocessing (projection evolution) is replay from the events
  table.** Today, state-at-T rebuild is provably equivalent to a full
  replay for the hot store: convergence (D10) makes final state a pure
  function of each resource's highest-sequence message, so both paths
  land identically. That equivalence **breaks for history-dependent
  projections** (change counters, churn rates, edge history).
  **Trigger:** when the first such projection arrives, build
  replay-from-cold (events scanned in sequence order, fed through the
  consumer's apply); until then, rebuild suffices and the property
  suite carries the proof.

### D4 addendum — cold budgets

| Budget | Target | Measured |
|---|---|---|
| Cold append throughput, batched | ≥ 10k events/s | **136.5k** @ batch 8192 (BENCHMARKS.md) |
| `state_at(T)` p50, 1M-event history, snapshots on | < 2 s | **0.39 s** (95% mark, BENCHMARKS.md) |
| Storage, 1M events | < 500 MB | **25 MB** @ batch 8192 (366 MB at batch 1024 — commit granularity is a storage decision; BENCHMARKS.md) |

All three validated with margin; no supersession needed.

## Phase contracts
- The generator MUST emit D2 messages exactly and expose `--seed`
  for reproducibility.
- The hot-store ingest MUST implement D3 as stated, with D10 apply
  semantics.
- The cold store MUST answer `state_at(T)` from its own tables alone
  (D12), with event-time semantics (D13).
- Any increment touching these contracts cites the D-number in its PR.


