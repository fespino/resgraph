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

**D2 addendum (phase 4, tightening — after King's
["Parse, don't validate"](https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/)):**
two coherence rules move into the parser, where the schema's other
invariants already live:

- **`resource_id`'s prefix MUST equal `resource_type`.** The invariant
  was unstated and relied on: the snapshot loader keys node labels off
  `resource_type`, the ingest off the id prefix — a message where the
  two disagreed parsed cleanly and would have materialized *different
  nodes* depending on which write path it took. That message is now
  unrepresentable.
- **`relationships[].target_id`'s prefix MUST be a known resource
  type.** Previously enforced mid-transaction inside the ingest apply
  (shotgun-style); now a parse error at the boundary. The remaining
  in-consumer checks demote to defense-in-depth where derived strings
  enter query text.

This *tightens* the accepted-message set: incoherent producers that
were previously half-accepted (inconsistently, per path) are now
rejected at parse time. The essay is the standing guide for this
schema: an invariant a consumer relies on belongs in the type, not in
the consumer — `label_for()` remains the parse point for the one
boundary raw strings still cross (user-supplied query ids).

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

**D14 addendum — apply-failure containment (#44).** Parse poison was
always handled (count, log, ack); a message that parses and then makes
the apply raise had no story: unacked batch, crash, redelivery of the
same batch — a deterministic apply failure was an infinite crash loop.
Three mechanisms close it, all in `StreamConsumer` so both stores
inherit them:

- **Retry with backoff, then binary split.** An apply exception fails
  the whole batch (batch atomicity is correct) but no longer kills the
  loop: the batch retries with exponential backoff; if retries
  exhaust, it splits in halves — one attempt each, recursing — until
  the poisonous entry is isolated. D3 makes the re-application safe:
  a replayed sub-batch cannot double-apply. Halving finds one poison
  in a 1024-entry batch in ~10 applies; item-by-item would take 1024.
  The split phase deliberately does NOT re-run the backoff ladder: the
  full batch already survived it, so a residual failure is treated as
  deterministic. A transient error surviving that far can wrongly
  dead-letter an innocent entry — accepted, because that failure is
  *visible and replayable* (the DLQ keeps the payload) while the
  alternative failure mode (retry forever) is an invisible liveness
  loss.
- **Delivery-cap quarantine.** In-process retries never bump Redis's
  delivery counter, so the cap (`XPENDING`'s `times_delivered` > 5)
  is purely a cross-restart guard: an entry that keeps arriving in
  the pending drain after repeated crashes goes to the dead-letter
  stream instead of another lap.
- **The DLQ is `<stream>:dlq`** — original payload, error, source
  entry id, delivery count. Apply-poison keeps its payload because a
  valid message that failed to apply may be replayable after a fix;
  parse-poison stays ack-and-drop (unparseable bytes have no replay
  story). Counters gain `dead_lettered`; nonzero means read the DLQ.

**Rejected:** item-by-item apply on failure (O(n) transactions at
batch 1024 vs O(log n)); unlimited retries (the crash loop this
fixes); automatic DLQ replay (replay is a human decision here — an
automated replayer would need its own design, though D3 already makes
manual re-ingestion of DLQ entries safe).
**Reversal condition:** if DLQ entries ever accumulate faster than a
human triages them, that is the trigger for the automated replayer —
designed then, with its own D-number.

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

**D11 amendment (phase 4, honesty pass):** with the phase built and
measured, the challenge "is Iceberg justified for an append-only log?"
gets its answer on the record: **not by current technical need.** Every
capability the queries exercise — atomic per-batch visibility (one file
per batch; a rename is equivalent), stats-based file pruning (DuckDB
does it from parquet footers), schema stability (engineered via JSON
strings, sidestepping evolution) — a plain parquet directory would
match, without the measured Iceberg-specific costs (metadata
amplification, compounding commit overhead). Native time travel is
explicitly rejected (D13). The decision stands on the D1-shaped
grounds: catalog/engine interop as the production exit path, latent
rollback/branching, and deliberate skill-building — named plainly, not
dressed as engineering necessity. The roadmap deepens *cold-store*
usage (as-of serving, trigger replay, dashboard time travel, DR
runbook) but all of it binds to the event-time layer, which is
format-agnostic; one future phase measures Iceberg commit I/O as a
*specimen*.
**Sharpened reversal condition:** if by the end of the serving-layer
phase no Iceberg-exclusive capability has been exercised — a second
engine reading the table, a rollback used in an ops drill, or schema
evolution via a `schema_version` bump — the format is decoration:
either exercise one deliberately or swap to plain parquet and record
the simplification.

**Resolution (phase 5, on deadline):** the serving-layer phase
exercised the first Iceberg-exclusive capability — a **second engine
reading the table**: DuckDB's iceberg extension (its own Iceberg
implementation, no pyiceberg) reads the events table from the metadata
file alone, and with `sql/cold_semantics.sql` loaded reproduces
`state_at(T)` exactly (test_second_engine.py). The exit-path claim is
now a passing test, not prose; the format stays. The remaining
candidates below stand for their own phases.

**Likely resolutions, identified in advance** (so the serving-layer
phase inherits a checklist, not a threat): (1) snapshot-pinned
pagination cursors in the API phase — a frozen view across pages of a
growing table is Iceberg-exclusive; (2) agent forensics — "what did
the system know when the agent acted" is a *commit-time* question
(D13's rejection covers world-time questions only), answered by
pinning the snapshot ID in the agent's run record; (3) a rollback
drill in the incident-runbook phase (bad batch → roll back →
re-consume → reconcile); (4) write-audit-publish if D12's compaction
trigger ever fires; (5) incremental snapshot scans as an
eviction-proof tail for slow subscribers (heals D12's maxlen
limitation for table-tailing consumers) — **with the coupling this
creates recorded now: `cold maintain` currently expires all
non-current snapshots, which would strand an incremental tailer;
retention must become subscriber-aware (expire nothing newer than the
slowest tailer's resume point) before (5) ships**; (6) the snapshot
log as change-management evidence in the compliance phase.

### D12 — Cold layout: an append-only event log plus derived snapshots

Two tables:

- **`events`** — every D2 message, append-only, one row per message:
  the D2 fields flattened, `attrs` and `relationships` as JSON strings
  (arbitrary keys stay schema-stable), partitioned by `day(event_time)`.
  *Recorded limitation (phase 4):* the day partition is **inert for
  fixture data** — simulated world time advances ~100 µs per event, so
  a million events span minutes and land in one partition. It costs
  nothing and becomes real when event time spans days; kept, with the
  admission that today it is decoration by the repo's own
  indexes-are-budgets rule.
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

## Phase 5 — the query layer

### D15 — API surface: one thin HTTP layer over both stores

FastAPI service (`resgraph.api`), read-only. The endpoint set is the
contract the agent phase will wrap as tools, so it is small and fixed:

| Endpoint | Store | Notes |
|---|---|---|
| `GET /resources/{id}` | hot | current state + outbound edges |
| `GET /blast-radius/{id}?depth=&filter=` | hot | D8 traversal, depth cap enforced at the API boundary too |
| `GET /history/{id}` | cold | per-resource message history (D12) |
| `GET /world?at=T&filter=` | cold | point-in-time state (D13) |
| `GET /diff?from=&to=` | cold | created / deleted / changed between two moments |
| `GET /blast-radius/{id}?at=T&filter=` | both | the composite — planner territory (D16) |

Normative response rules:

- Every list endpoint carries a **result budget**: max 1000 rows,
  with `truncated: bool` and `total_count` so a capped answer is
  visibly capped, never silently short.
- Every response carries `fetched_at` (wall time of serving) and
  `source: hot | cold | composite` — a caller must be able to tell
  which clock and store produced an answer (D13 makes this
  distinction user-visible; hiding it would re-create the two-clocks
  confusion at the API boundary).
- `?explain=true` on planner-backed endpoints returns the plan
  **without executing** — no store is contacted.

**Rejected:** exposing the stores' native query languages
(Cypher/SQL passthrough) — maximum power, but it couples every caller
to store choice, defeats result budgets, and hands the future agent
phase an injection surface instead of a tool surface.
**Rejected:** GraphQL — resolver flexibility is exactly the unplanned
query shape the budgets exist to prevent; a fixed endpoint table is
the budget.
**Reversal condition:** when the agent phase needs a question this
table cannot express, extend the filter DSL or add an endpoint —
never a passthrough. If endpoint count outgrows a screen, that is the
moment to revisit the surface design, not before.

**D15 addendum — challenged: why not a database wire protocol as the
go-to surface?** The alternative: speak Postgres wire / ODBC so
SQL-native tooling (BI, notebooks, psql) connects with zero client
work. Recorded answer:

- The platform **already has** a zero-integration surface per store,
  at the layer below: the hot store speaks Bolt (any Neo4j client
  connects today), and the cold tables are open-format Iceberg on
  disk — any engine reads them without permission (proven by the
  second-engine interop test). What no wire protocol carries is the
  *semantics*: dedupe on `(resource_id, sequence)`, tombstones,
  event-time as-of (D13). A raw SQL surface hands every consumer the
  obligation to re-implement D13 — the silent-wrongness class it
  exists to prevent.
- So the split is by persona, not by protocol: **agents and
  programmatic consumers** get the governed HTTP surface (budgets,
  caps, labeled sources — the passthrough rejection above is about
  them); **SQL-native tooling** gets the open format, with the
  semantics shipped alongside as SQL (`sql/cold_semantics.sql`, a
  `state_at(t)` macro any DuckDB-dialect engine can load) instead of
  locked behind endpoints.
- **Rejected for now:** running a Postgres-wire front (pgwire proxy,
  pg_duckdb/pg_lake, Flight SQL server) — a third surface to operate,
  with no SQL-native consumer in the platform today.
- **Trigger:** the first real BI/ODBC consumer. Then front the *cold
  half* with a SQL-serving engine (pg_duckdb/pg_lake, or Trino if
  D16's adopt-line has fired by then) and load the shipped semantic
  views into it. Do not teach the HTTP API to speak SQL.

### D16 — Mini planner: predicate push-down across two stores

Scope: a filter DSL of **conjunctions only** —
`type=vm AND attrs.zone=z1` (equality + numeric comparison; no OR, no
functions). Scope is the skill: two backends warrant a placement
table, not an optimizer.

- **Placement rule:** each predicate goes to the store best able to
  evaluate it — type and known-attr filters compile into Cypher
  `WHERE` (hot) or DuckDB `WHERE` (cold, `json_extract` on the attrs
  column) depending on route; anything neither store claims becomes a
  **residual filter** applied in Python and *flagged in the plan* — a
  visible residual is a design smell you can see and measure.
  The set of known attr fields is derived from the generator's
  `ATTR_POOLS` (the D5 discipline again: the table is code, not
  prose), so the placement table cannot drift from the world's
  actual schema.
- **Composite strategy ("blast radius as of T"):** reconstruct the
  as-of-T world from cold (D13 `state_at`, predicates pushed into the
  DuckDB scan), build an ephemeral in-memory digraph from the
  surviving rows' relationships, BFS there with the same
  dependent→dependency direction and depth cap as D8.
  **Rejected:** time-traveling the hot store — it has no history;
  that is D13's whole division of labor.
  **Rejected:** loading as-of-T into a scratch Memgraph label-space —
  heavier, stateful, and the ephemeral graph is bounded by the same
  result budgets anyway.
- **Lazy by construction:** `plan()` returns a `Plan`; nothing
  touches a store until `.execute()`. `explain` is the plan
  serialized, executed never.

**Rejected:** a real parser + cost-based optimizer — theater at two
backends. The placement table becomes a cost decision the moment two
placements are both *possible* and statistics must choose; that is
the build-vs-adopt line.
**Reversal condition:** a third store, or the first placement choice
that needs row statistics to make — either is the signal to adopt a
federated engine (Trino-shaped) rather than grow one here, and the
decision to adopt gets its own D-number.

**D16 addendum (reviewed against Grove's [*How Query Engines
Work*](https://howqueryengineswork.com/)):** two findings from reading
the book this phase's vocabulary doc cites.

- **Projection push-down was the missing half.** The book's
  data-source contract is `scan(projection)` — column pruning is the
  baseline, stated *before* predicates; its optimizer's first
  implemented rule is projection push-down. This planner pushed
  predicates and returned every column, JSON-parsing `attrs` for
  thirty thousand rows to answer with four — exactly the
  Arrow-boundary tax the phase's own benchmark named as the dominant
  cost. Placement now has both halves: predicates (`where` +
  `where_cols`) and columns (`projection`), both pruned at the Iceberg
  scan so unrequested columns never cross the boundary. Measured:
  composite p50 at 1M events 0.371 s → **0.250 s** (BENCHMARKS.md).
- **The Plan is a logical plan with the physical choice pre-made.**
  The book separates logical from physical plans for three stated
  reasons — per-operator algorithm choice, environment adaptation,
  cost-based selection among candidates — and all three are absent
  here by design: one algorithm per route, one environment, statistics
  refused. `Step.detail` carrying compiled Cypher/SQL at plan time is
  that collapse, made deliberately; the reversal condition above (the
  first stats-needing choice) is precisely the moment a separate
  physical layer would earn its existence. The book's own teaching
  engine concedes the point in practice — its planner makes fixed
  physical choices; the separation is pedagogy until there are real
  alternatives to choose between.

### D4 addendum — query-layer budgets (provisional)

| Budget | Target | Measured |
|---|---|---|
| live endpoints (hot), p50 server-side | < 100 ms | **2.5 ms** end-to-end (BENCHMARKS.md) |
| composite as-of blast radius, 1M-event history, p50 | < 2 s | **0.250 s** after projection push-down; 0.371 s before (BENCHMARKS.md) |
| `explain=true`, any endpoint | < 50 ms, zero store contact | **0.012 ms**; zero-contact asserted by test |
| residual-filter delta | measured and reported, no target — it is the push-down argument's evidence | **2.5× at 1M events** — and it grows with scale (BENCHMARKS.md) |

The composite budget rides on the D4 cold budget (`state_at` 0.39 s
at the 95% mark): reconstruction dominates, traverse and serialization
must fit in the remainder.

## Phase contracts
- The generator MUST emit D2 messages exactly and expose `--seed`
  for reproducibility.
- The hot-store ingest MUST implement D3 as stated, with D10 apply
  semantics.
- The cold store MUST answer `state_at(T)` from its own tables alone
  (D12), with event-time semantics (D13).
- The API MUST enforce result budgets and label every response with
  its store of origin (D15); planner-backed endpoints MUST be able to
  explain without executing (D16).
- Any increment touching these contracts cites the D-number in its PR.


