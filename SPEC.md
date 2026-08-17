# resgraph SPEC

Decision log + phase contracts. Locked decisions carry D-NN ids; changing
one requires a new decision superseding it, not an edit.

Decisions are ordered by D-number, not by phase: the decision is the
entity, the phase is when events on it happened. Each decision's heading
names the phase that made it, and its amendments live inside its own
section as dated, phase-stamped blocks in event order. The index below
maps each phase to its events.

## Phase index

| Phase | Decided | Amended |
|---|---|---|
| 0 — foundations | D0–D4 | — |
| 1 — world generator | D5–D7 | D4 (emit budget) |
| 2 — graph hot store | D8–D9 | — |
| 3 — streaming ingest | D10, D14 | D4 (ingest budget) |
| 4 — cold store | D11–D13 | D2 (parse tightening), D11 (honesty pass), D4 (cold budgets) |
| 5 — query layer | D15–D16 | D4 (query-layer budgets) |
| 6 — observability | D17–D18 | D14 (outage is not poison) |
| post-6 (2026-08-02) | — | D0 (pyright strict gate, #68) |
| 7 — MCP server | D19–D21 | — |
| 8 — analyst agent | D22–D25 | `phase-8-analyst` |

## D0 — Toolchain: typed Python, with the types enforced (phase 0)

Python 3.13 + uv + ruff from day one; **pyright in strict mode joined
as a required CI gate** (2026-08-02, #68) once the codebase's
constructive style — closed `Literal` sets, frozen models, sum-type
dispatch — had no machine checking it. What the gate enforces beyond
annotations: exhaustiveness. Closed types (`Op`, `RelType`,
`Predicate.op`, plan kinds) dispatch through `match` with
`typing.assert_never` on the fall-through, so adding a variant is a
compile-time obligation on every consumer, not a hope that a test
looks. Config honesty: the `reportUnknown*` rule family is off — it
measures third-party stub quality (neo4j, pyiceberg, duckdb, redis),
not this repo's correctness; each is re-enabled as stubs complete.
Composed Cypher goes through one `lit()` chokepoint whose docstring
carries the injection argument (identifiers from closed sets, values
as bound parameters).
**Rejected:** mypy (weaker inference on the Pydantic/FastAPI/Typer
patterns this repo lives in); gradual adoption with a suppression
baseline (at ~1,500 statements, strict-in-one-PR is cheaper than a
ratchet and hides nothing); `ty`/`pyrefly` (the Rust generation —
right shape, pre-1.0 at adoption time; a merge gate does not pin to
a beta).
**Reversal condition:** re-evaluate the checker (not the gate) when
Astral's `ty` ships stable — same config surface, faster; the gate
itself only tightens.

## D1 — Graph hot store: Memgraph (Community) (phase 0)

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

## D2 — Update message schema (verbatim; the generator/ingest contract) (phase 0)

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

## D3 — Idempotency: per-resource applied-sequence watermark (phase 0)

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

## D4 — Performance budgets (provisional until ingest baselines exist) (phase 0)

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

#### D4 addendum (phase 4) — cold budgets

| Budget | Target | Measured |
|---|---|---|
| Cold append throughput, batched | ≥ 10k events/s | **136.5k** @ batch 8192 (BENCHMARKS.md) |
| `state_at(T)` p50, 1M-event history, snapshots on | < 2 s | **0.39 s** (95% mark, BENCHMARKS.md) |
| Storage, 1M events | < 500 MB | **25 MB** @ batch 8192 (366 MB at batch 1024 — commit granularity is a storage decision; BENCHMARKS.md) |

All three validated with margin; no supersession needed.

#### D4 addendum (phase 5) — query-layer budgets (provisional)

| Budget | Target | Measured |
|---|---|---|
| live endpoints (hot), p50 server-side | < 100 ms | **2.5 ms** end-to-end (BENCHMARKS.md) |
| composite as-of blast radius, 1M-event history, p50 | < 2 s | **0.250 s** after projection push-down; 0.371 s before (BENCHMARKS.md) |
| `explain=true`, any endpoint | < 50 ms, zero store contact | **0.012 ms**; zero-contact asserted by test |
| residual-filter delta | measured and reported, no target — it is the push-down argument's evidence | **2.5× at 1M events** — and it grows with scale (BENCHMARKS.md) |

The composite budget rides on the D4 cold budget (`state_at` 0.39 s
at the 95% mark): reconstruction dominates, traverse and serialization
must fit in the remainder.

## D5 — World topology (allowed edges) (phase 1)

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

## D6 — Determinism contract (phase 1)

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

## D7 — Churn model (phase 1)

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

## D8 — Graph modeling (phase 2)

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

## D9 — Dangling edges: phantom nodes (phase 2)

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

## D10 — Apply-time state semantics (phase 3)

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

## D11 — Cold store engine: Iceberg via pyiceberg, queried through DuckDB (phase 4)

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

## D12 — Cold layout: an append-only event log plus derived snapshots (phase 4)

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

## D13 — Time travel is event time, not commit time (phase 4)

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

## D14 — Stream consumption model (phase 3, recorded retroactively)

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

**D14 addendum, superseded in part (phase 6, #54):** the containment
above conflated two failure classes the drill was designed to expose:
*this message is bad* and *the store is down*. A multi-minute outage
would walk the entire in-flight backlog through retry → split → DLQ —
quarantining healthy messages because their store was napping.
Superseding rule: **outage is not poison.** Consumers declare
connection-class exceptions (`retryable_exceptions`; the hot consumer
passes the Bolt driver's ServiceUnavailable/SessionExpired/
TransientError); those retry forever with capped backoff and never
count toward the retry ladder, never split, never dead-letter — the
messages are fine, the store will return, and D3 makes the eventual
re-apply safe. Only apply-class errors walk the poison path.
Acceptance: store down for minutes under load → DLQ stays flat
(test_consumer_dlq.py, time-compressed; the drill runs it for real).
**Rejected:** a circuit breaker pausing XREADGROUP — equivalent
liveness, more state; the blocked apply already provides backpressure
by construction (the consumer reads nothing while holding a batch).

## D15 — API surface: one thin HTTP layer over both stores (phase 5)

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

## D16 — Mini planner: predicate push-down across two stores (phase 5)

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

## D17 — Telemetry: wide events are the log, metrics are a view (phase 6)

The repo's thesis applied to its own telemetry: a pre-aggregated
counter is state *without* a log — once incremented, the event that
incremented it is gone, along with every question not asked in
advance. So telemetry gets the D12 shape, one level up:

- **Wide structured events are the primary telemetry.** One JSON
  event per API request and per consumed batch — everything known
  about the unit of work, not just the numbers pre-chosen for charts.
  Sunk as appended NDJSON files under `data/telemetry/`,
  DuckDB-queryable like everything else this platform calls truth.
  Deliberately **not** shipped through the Redis stream: telemetry
  must never share fate with the transport it watches.
- **Prometheus metrics are the derived alerting view** — bounded
  labels, short retention, disposable by design. It fills the slot
  Memgraph fills for data: a hot serving-and-evaluation layer over a
  log we own. Adopted rather than built for two reasons: continuous
  rule evaluation the platform does not have, and failure-domain
  separation — the phase-6 drill kills stores on purpose and the
  alert must still fire; an observer hosted on the observed dies
  with it.
- **The dependency direction is a test, not a stance:** both D18 SLIs
  recomputed from the raw events in SQL must agree with the
  Prometheus-computed ratios within scrape tolerance (the parity
  test). If they disagree, one layer is lying.
- **Instrumentation via the OpenTelemetry metrics API + SDK**,
  Prometheus pull exporter, no collector — the D1 transferability
  argument applied to telemetry: the standard API is the durable
  skill, the backend a detail. Metric names stay ours where OTel
  semantic conventions would fight dashboard clarity.
- **OTel GenAI conventions: adopted for concepts, not for names**
  (amended #152). The
  [`gen_ai.*` span attributes](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
  postdate D17's metric names and cover the shape this platform already
  records — model, token counts, operation. The names stay ours
  (`analyst_run_seconds`, `analyst_run_cost_usd`) because the dashboard
  and the SLO rules are keyed to them and a rename is a silent-dead-panel
  risk for no measurement gain. Recorded rather than left silent, per
  this decision's own rule. **Reversal condition:** an OTel-native
  backend or a collector that routes on `gen_ai.*` makes the convention
  load-bearing rather than cosmetic.
- **Stack as code:** Prometheus + Grafana under the compose `obs`
  profile (opt-in — the observer must not distort the benchmarks it
  observes), images digest-pinned, scrape config + datasource +
  dashboard provisioned from committed files.

- **Event content policy:** events carry identifiers, dimensions,
  timings, and counts — never payload bodies (no message attrs, no
  free-text beyond the already-bounded query string). What an event
  references, the platform's stores can look up; what an event
  *contains* is forever, greppable, and — once the agent phases tail
  these files — part of a prompt. Guard test: no event field value
  exceeds 600 characters.

**Rejected:** metrics-only (the standard recipe; aggregation-first
destroys the raw events — inconsistent with the log-first thesis, and
the coming agent phases consume events, not counters — an agent
cannot drill into a counter). **Rejected:** events-only (alerting
over event queries means hand-rolling an always-on evaluator; two
SLOs do not justify replacing burn-rate machinery that is cheap,
bounded, and explainable). **Rejected:** `prometheus_client` directly
(tool-specific API where a standard exists). **Rejected:** the OTel
Collector (a translation daemon between one process and one
Prometheus on one host is decoration; adopt-triggers: OTLP push, a
second backend, or traces). **Rejected:** per-entity metric labels,
permanently — cardinality discipline starts on day one.
**Reversal condition:** the roadmap's reactive-trigger phase builds
the always-on evaluator this design borrows from Prometheus; when it
lands, re-evaluate migrating SLO evaluation onto the platform itself.

#### D17 addendum — instrument coverage, decided not defaulted (#60)

The phase-6 build dropped two planned instrument groups without a
recorded decision; this addendum is that decision.

- **Built: `graph_phantoms_created` (counter).** Phantom creation is
  observable exactly and for free: the edge-apply MERGEs can only
  ever create phantom target nodes (sources are MATCHed), so the
  write summary's `nodes_created` *is* the phantom count — no
  scrape-time store query needed.
- **Rejected: a current-phantom-count gauge.** Reading it live means
  the metrics path querying the observed store at scrape time —
  exactly the observer/observed coupling the failure-domain argument
  above forbids. The current count is a store fact, available on
  demand (`queries.stats` reports it) and derivable from the counter
  plus resolutions when history matters.
- **Deferred: cold-writer instruments** (queue depth, rows per
  flush). Cold-path health is already triangulated by consumer lag,
  the drill's drain gates, and reconcile — a queue-depth panel would
  be furniture, not signal, until an incident says otherwise.
  Reopen trigger: the first cold-path incident the existing signals
  miss.

## D18 — Two per-run SLOs, ratio SLIs, thresholds derived not invented (phase 6)

Method per the SRE Workbook's SLO-implementation chapter. SLO
document fields: author F. Espino (solo — the gates are the
stakeholders); adopted 2026-08-01; derivation from observational
baselines (BENCHMARKS.md), flagged as such; reviewed at every phase
closeout via the met/missed × toil decision matrix.

**SLO 1 — ingest freshness (pipeline).**
- *SLI specification:* the proportion of time the ingest is no more
  than ~3 s behind the producer.
- *SLI implementation:* proportion of scrape intervals where
  `ingest_lag` ≤ **31,500 messages** (= 3 s × 10,500 sustained
  updates/s, BENCHMARKS.md ingest table, the longer-run number not
  the sweep peak). Blind spot: lag reads the broker's view
  (`XINFO GROUPS`); producer-side delay before `XADD` is invisible
  to it.
- *Objective:* **99%** of scrape intervals, per run (a load session
  ≥ 30 min).

**SLO 2 — composite query latency (request-driven).**
- *SLI specification:* the proportion of composite blast-radius
  requests answered fast enough not to matter (< 1 s perceived).
- *SLI implementation:* `api_request_seconds` histogram bucket
  `le="0.6"` over total composite requests — the bucket boundary
  sits AT the threshold, so good events are counted exactly, never
  interpolated from a quantile. Threshold derivation: 1.5 × measured
  composite p95 0.393 s (BENCHMARKS.md) = 0.59, rounded to 0.6 s.
  Blind spot: server-side; network and client rendering excluded.
- *Objective:* **95%** of requests, per run.

**Window honesty:** per-run windows, stated loudly — the canonical
4-week rolling window (and its weekend-parity argument) cannot apply
to a laptop that sleeps at night. Consequence: only the fast-burn
alert tier exists; with per-run windows the slow-burn tier has
nothing to measure.

**Error budget policy (the teeth):** budget = 100% − objective,
per run. A load run that exhausts either budget fails its run
report and opens an issue; the chaos drill does not pass unless the
freshness fast-burn alert actually fired; nonzero `dead_lettered`
in any run report means read the DLQ before trusting the run.
Incidents are quantified as the % of the run's budget they consumed
(INC-001 carries the first such number).

**Rejected:** quantile-threshold SLIs (`p99(lag) < X`) — the ratio
form (good events ÷ valid events) is what makes the budget
arithmetic and incident costs comparable; the Workbook's counting
preference over histogram approximation is implemented literally via
the bucket-at-threshold trick. **Rejected:** SLO-as-code generators
(OpenSLO/Sloth/Pyrra — the current industry default): two hand-rolled
rules with visible derivations teach more than a generator hides;
adopt-trigger: a third SLO or multiwindow multi-burn alerting.
**Reversal condition:** the closeout decision matrix — an SLO met
with margin and zero toil tightens; an SLO missed by an honest
system loosens; either change is a new D18 revision, never a silent
edit.

#### D18 addendum — two honesty notes from the closeout review (#60)

- **Fast-burn multiplier: 6×, not the canonical 14.4×.** The 14.4×
  fastest tier assumes a multi-week window with hours-scale budget
  consumption; per-run windows re-derive from the same principle —
  "budget exhausted in ≤5 minutes of a 30-minute run" — and that
  arithmetic gives 6× (derivation as a comment in
  `observability/rules/slo.yml`). Same rule, different window, shown
  work.
- **Policy scope, stated plainly:** of the budget policy's teeth,
  only the drill gates are code today — "a load run that exhausts
  its budget fails its run report" has no run-report harness behind
  it yet. The clause binds drills until a load-run harness exists
  (expected alongside #32's scale-out benchmarking); when it lands,
  the report gate becomes code or this addendum reopens.

## D19 — Tool surface: registry-canonical, task-shaped, dual-surface (phase 7)

Canonical tool bodies live in `src/resgraph/tools/canonical/` as plain
functions — Pydantic input model, Pydantic output model, and a
keyword-only `CallerContext` the LLM never sees (transport-injected,
absent from the LLM-facing schema, so a caller cannot supply its own
authority). One `TOOL_REGISTRY` declares the surface; the MCP server
and the HTTP `/tools/{name}` routes both derive from the same loop.
The transport is never the truth-bearing module, and the drift guard
(four AST-level CI assertions) makes a second definition structurally
impossible rather than currently absent.

Tools are TASK-shaped, not route-shaped: `blast_radius`,
`dependency_path`, `resource_history`, `world_diff`, `fetch_resource`.
An agent investigating an incident wants "blast_radius(db-42)", not
five REST calls it must orchestrate itself. The discipline has a name
— agent-computer-interface poka-yoke
([Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)):
make the mistake structurally hard, not instructed-against.

The registry carries production seams from day one, all metadata-only
at v1: `scopes` (`resgraph:read`), `privileged` (admission rule: a
tool is privileged iff it mutates platform state, is only legitimate
inside an approval workflow, and external clients have no bypassing
use case — v1's answer is "none", the seam costs one field), the four
MCP risk-annotation hints (`readOnlyHint`/`destructiveHint`/
`idempotentHint`/`openWorldHint` — declared explicitly on every tool
because the
[spec defaults](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)
read as destructive + open-world, and risk composes per session, not
per tool), `timeout_s`, and a structured error-action map
(rephrase / retry / give up) per the gaps named in the
[MCP enterprise field report](https://arxiv.org/abs/2603.13417) —
`timeout_s` is the laptop-scale simplification of their adaptive
budgeting. Retrofitting any of these means reopening every tool;
flipping enforcement on an existing field is one middleware.

Pinned to MCP spec revision **`2026-07-28`** (stateless core). All
five tools are single-shot reads — no session state, no handles —
which is exactly the shape that revision asks for; skills-as-prompts
and the server card ride primitives unaffected by its deprecations.

**Named alternative (recorded, with trigger):** Microsoft's
[Azure SRE Agent](https://techcommunity.microsoft.com/blog/appsonazureblog/context-engineering-lessons-from-building-azure-sre-agent/4481200/)
collapsed 100+ narrow tools to ~5 WIDE ones (az/kubectl as whole CLI
ecosystems) — endorsing the small count while disputing the shape.
Their winning argument (lean on the model's training-data knowledge of
those CLIs) does not transfer to a bespoke surface no model has seen,
so task-shaped stands. Trigger: if agent traces show improvisation
around the surface — questions the five tools cannot compose — test a
wide/files-based arm before adding tool six.

**Second named alternative (recorded, with trigger):** composition in
code —
[code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
presents tool surfaces as code APIs so the agent writes a script;
intermediate results stay in the execution environment (their
benchmark: 150k → 2k tokens, 98.7%), converging with Azure SRE's
tool-call chaining (60–70% projected). The known first candidate here
is already shipped: the change-forensics playbook intersects
world_diff refs with blast_radius refs in context — a set operation
flowing through the model. Trigger: when traces show rote multi-tool
sequences or in-context set operations over ref lists, evaluate
executing composition registry-side — either a task-shaped composed
tool, or agent-written code once a sandbox exists (its stated cost:
secure execution, resource limits, monitoring — a later phase's
machinery, not this one's).
**Rejected:** porting the HTTP API 1:1 (route-shaped tools push
orchestration into the agent); separate schema definitions per surface
(precisely the drift class this decision exists to kill).
**Reversal condition:** the named-alternative trigger above; also
re-pin when the 2026-07-28 revision leaves RC if anything binding
changed.

**Addendum (2026-08-15, #166 item 5 discharged as adopted):** the
revision's `ttlMs`/`cacheScope` are required fields on every list/read
*result* (`CacheableResult`) on every transport — not, as #166 first
filed, a concern for the day this surface speaks HTTP. Unhinted, the
SDK fills them with `0`/`"private"`: conformant on the wire, but a
deploy-static catalog served "immediately stale" defeats the caching
the fields exist for. Adopted: one `CacheHint(ttl_ms=300_000,
scope="public")` across all six cacheable methods. Five minutes, not
longer: the catalog changes only at deploys, so the TTL is exactly the
post-deploy window in which a client may serve the old catalog — and
this is a dev-iterated server, while the caching benefit of a longer
TTL is negligible (an in-process list over stdio). `"public"` because
the surface does not vary by authorization context. Asserted
end-to-end in the protocol test.

## D20 — Budgets inside the tool: refs+fetch, token caps, freshness (phase 7)

The agent must not be able to ask an unbounded question, and
enforcement lives server-side, not in prompt etiquette:

- **Clamps, not errors.** `depth` beyond the traversal cap clamps and
  says so (`depth_clamped: true`). An agent that gets clamped keeps
  working; one that gets a 500 retries in a loop.
- **Refs+fetch.** Traversal and diff responses return bare refs
  `{id, type, one_line}`; one polymorphic `fetch_resource(id, at=?)`
  returns detail for any resource type. A 400-node blast radius with
  full attributes is a blown context window the agent cannot steer;
  refs let it fetch the three nodes it cares about. Independently
  confirmed in production by Azure SRE's "treat large tool outputs as
  data sources, not context."
- **Token cap.** Every response serializes under
  `TOOL_RESPONSE_TOKEN_CAP = 8000` (len/4 estimate — the ceiling is
  the point, not the precision). Overflow paginates: `truncated:
  true`, honest `total_count`, and a prose `pagination_hint` ("call
  again with offset=N") — the consumer is a language model, so the
  payload teaches the next move. Pagination is an argument, not a
  separate tool.
- **Freshness.** Every response carries `fetched_at` and
  `source: hot|cold|composite`, propagated from the query layer
  untouched. An agent reasoning over a 20-minute-old payload about a
  live system needs to know to re-fetch; freshness IS correctness when
  the world churns.
- **Errors are steering surfaces.** A rejection says what to do
  instead, in the message — not in documentation the model never
  loads. [Stripe's steering experiments](https://stripe.dev/blog/ai-steering-experiments)
  measured the asymmetry: passive documentation is ignored ("agents
  simply don't wander"), while error-based steering reliably corrects
  behavior.

**Rejected:** fat traversal responses (see refs+fetch above); a
separate `_page` tool (tool-selection where an argument suffices);
erroring on over-cap depth (the retry-loop failure mode).
**Reversal condition:** when callers with different context budgets
exist, the cap moves from a constant into the protocol conversation
(client declares, server shapes).

## D21 — Skills as prompts: validated manifest, fixed six-section body (phase 7)

Investigation playbooks ship as `skills/<slug>/SKILL.md` — YAML
frontmatter validated by a Pydantic manifest, exposed over MCP as
prompts. `tool_refs` are validated against `TOOL_REGISTRY` at load: a
skill referencing a tool that doesn't exist **fails loudly at
startup**, never silently at runtime.
**Rejected:** soft-fail (exclude the bad skill and log) — at two
skills, a silently missing playbook is a worse failure than a crashed
startup; revisit only if skills become a governed corpus with
independent authorship.

Body: six sections in fixed order — Goal, When to use, Steps, Tools to
call, Examples, Anti-patterns — positional consistency so the
consuming LLM learns the shape once. Playbooks state constraints
before narrative (budget discipline in the skill, not just the tool),
the format
[Stripe's grounding-file work](https://stripe.dev/blog/build-stripe-salesforce-integrations-faster-with-agents)
validated: a constraint-first core (rules, signatures, failure modes —
not tutorials) took a task from hours of failed iterations to minutes.
v1 ships two skills deliberately different in shape: `incident-impact`
(workflow) and `change-forensics` (analytical). The count stays small
on evidence: [LangChain measured](https://www.langchain.com/blog/evaluating-skills)
82% task completion with curated skills vs 9% without — and
wrong-skill selection appearing at ~20 similar skills, so similarity
at scale is the ceiling, not count alone.
**Reversal condition:** if prose playbooks plateau in an evaluation
loop, the named next experiment is compiling them to schema-validated
step graphs ([AIP](https://arxiv.org/abs/2606.04781), 📄 Paper:
arXiv:2606.04781 — 53%→67% with step-level repair).

## D22 — Architecture: one agent, tools over handoffs (phase 8)

`resgraph-analyst` is a single agent on the Anthropic API with the D19
tool surface. No multi-agent graph. In the field's shared vocabulary
([Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)):
this program already runs **evaluator-optimizer** (the iteration loop,
with deterministic graders as the evaluator) and rejects
**orchestrator-workers** until evidence admits it — the vendor's own
bar, "only add complexity when simpler solutions fall short," is this
decision's admission rule, so the single agent is the field's default,
not an idiosyncrasy.

**Sub-agent admission rule:** a sub-agent earns existence only if it
has specialized tools, a constrained scope, or an authorization
boundary the primary cannot hold — formatting or "analysis" alone
never qualifies; that workload belongs in the primary's prompt.
resgraph-analyst has one scope, one toolset, one authority level: one
agent. The null hypothesis has teeth: reasoning-tier models already
run an internal deliberation
(📄 Paper: [Societies of Thought](https://arxiv.org/abs/2601.10825) —
arXiv:2601.10825), so any decomposition must beat a reasoning-tier
single agent on scenarios built to break it, and report its cost
multiple — evidence recorded in a future decision, not assumed.

Tool wiring: Anthropic `tools=[...]` blocks derive from TOOL_REGISTRY
— a third surface next to MCP and HTTP; the drift guard grows an
assertion to cover it. In-process for the harness: a same-process MCP
transport would add framing for no boundary, and the phase-7
integration suite already proves protocol parity for external callers.

Budgets live in the harness, not the prompt: max 15 tool CALLS (calls,
not turns — parallel fan-out inside one turn is encouraged where the
plan allows it; 📄 Paper: [W&D](https://arxiv.org/abs/2602.07359) —
arXiv:2602.07359) and a token ceiling per run. Exhaustion is not an
error: the agent concludes with what it has and marks the run
`degraded: true`.
**Rejected:** an agent framework — the harness is ~300 lines of
visible control flow, and the loop being engineered is the deliverable;
an abstraction that hides it defeats the phase. Build-vs-adopt gets a
written comparison when the decomposition experiment runs.
**Rejected:** multi-agent as the default — N× the cost for the same
answer on single-cause scenarios.
**Reversal condition:** the decomposition experiment's evidence gate —
if a single agent measurably fails on multi-cause/confounded scenarios
and the smallest decomposition beats it there, the split runs on
exactly those incident shapes, priced.

## D23 — Prompt and context architecture: cache-aware from birth (phase 8)

- **Prefix/suffix split, committed audit table:** every prompt section
  gets a PREFIX (static, cacheable) / SUFFIX (runtime) verdict —
  identity, triage discipline, tool guidance, output schema → PREFIX
  with a `cache_control` breakpoint at its end; alert payload + world
  summary → SUFFIX. No hedging sections into SUFFIX "because they
  might change."
- **Message-order invariants (test-enforced):** first turn =
  `[prefix (cached)] → [suffix] → [user]`; retry feedback appends as a
  NEW message, never edits the system prompt — a byte-changed prefix
  busts the cache on exactly the runs that need retries most. A test
  asserts prefix bytes are identical across retry attempts.
- **Thinking blocks are preserved verbatim** when tool results are
  passed back — omitting them silently degrades multi-step reasoning
  and the evals would misattribute the damage to the model
  ([extended thinking docs](https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking));
  a test asserts the replayed message shape, and the thinking
  configuration is part of the pinned eval environment.
- **Tool schemas are implicit prefix** — they serialize before the
  breakpoint, so editing a tool description busts the cache while the
  prompt file shows no diff. Documented next to the metric so future
  debugging starts at the right artifact.
- **Critical rules live only in compaction-safe positions** — nothing
  load-bearing goes where a context-management pass could silently
  drop it.
- **Context strategy:** a compact world summary up front (~500 tokens:
  resource counts by type, the alert resource's immediate neighborhood
  as bare refs, event-window bounds); everything else ID+fetch on
  demand through the D20 tools. **Reversal is evidence-gated:** if
  eval traces show ≥30% of tool calls re-fetching what a richer
  summary would have carried, widen the summary and re-measure.
- **Metric:** token-weighted cache hit rate = Σ cache_read / Σ input
  per run, in every eval report, target ≥ 0.9 on multi-turn runs.
  Token-weighted, not call-count: one hard miss must be visibly
  expensive.
- **Amended 2026-08-03 (baseline run 1, EVALS.md iteration 2):** the
  prefix breakpoint alone is insufficient — the growing transcript
  re-bills as plain input every turn, so the metric structurally
  cannot reach 0.9 on multi-turn runs. The harness adds one moving
  breakpoint on the last message block of each request; the
  audit-table method missed this because the transcript is the one
  section that exists only at runtime.
- **Amended 2026-08-03, second (iteration 2's outcome):** the
  discipline gate is the **uncached re-read fraction ≤ 0.1** (plain
  input / total input), not the cache-hit floor. With re-billing at
  zero, short runs still fail 0.9 because the residue is
  `cache_creation` — the one-time write every new token owes, which
  is cost but not waste. A gate may only penalize what the harness
  can avoid. The token-weighted cache-hit rate remains reported in
  every run row; see the correction row dated 2026-08-03.

## D24 — Eval contract: ground truth first, judge last (phase 8)

Dataset: `evals/scenarios/*.jsonl`, one item per line — id,
description, alert, seed + generator args (the world is reproducible
from the item), ground_truth {causal_sequence, mechanism_path},
provenance {source: planted | failure_derived, notes}, tags. Failure-
derived items are permanent: every iteration failure becomes a
regression item whose provenance points at the run that exposed it.

Five dimensions, every item, every run:
1. **Found** — planted change in top-1 / top-3 (sequence-id compare).
2. **Evidence verifiable** — every mechanism-path edge existed in the
   graph in the scenario window (composite as-of query); every cited
   event exists in the cold log. The graders are production queries
   reused — a grader bug IS a platform bug, one fix serves both.
3. **Honesty** — controls must conclude "no confident candidate";
   high-confidence-wrong scores worse than honest-miss everywhere.
4. **Discipline** — budgets respected; no identical repeated calls;
   structured output parsed first try; cache ≥ 0.9 — and the
   trajectory is graded, not just the outcome: a pass that arrives
   through blind retries or an unverified guess is reported as a
   lucky pass, separately from clean passes (up to 23.2% of agent
   passes are lucky under process scoring — 📄 Paper:
   [AgentLens](https://arxiv.org/abs/2605.12925) — arXiv:2605.12925).
5. **Narrative judge** — pinned (model and template; any change is a
   labeled baseline-refresh event; the pin originally included
   temperature=0 and a seed, but the API rejects temperature on this
   model generation and never exposed a seed — a pin can only contain
   knobs the API accepts, see Corrections 2026-08-03),
   injection-hardened (content inside tags is data), smallest weight,
   prose quality only. **Rejected:** judge-graded correctness —
   grading with an LLM when the domain hands you ground truth is the
   named pitfall, and dims 1–4 never hallucinate.

The report also carries: a **calibration table** — empirical accuracy
per emitted confidence level, per slice; high must beat medium must
beat low or the confidence field is decoration.
**Superseded in part (2026-08-04, phase 8):** the abstention flag is
now derived from checkable verdicts rather than emitted confidence
(EVALS.md, iteration 7 — self-assessments measured as performative),
so the calibration table's subject narrows to the still-emitted
per-suspect confidence field; it activates with the k=3 data (#96) (verbal confidence is
not a credence readout — 📄 Paper:
[attribution of confidence](https://arxiv.org/abs/2407.08388) —
arXiv:2407.08388 — so the field earns meaning behaviorally, which the
planted-difficulty ground truth makes measurable). And a **paired
skill arm**: scenarios run with and without the D21 playbook loaded,
same model and environment, and the intervention is ledgered in four
stages — available, retrieved, invoked, relevant
([lopopolo's harness-eval framework](https://github.com/lopopolo/harness-engineering/tree/trunk/evals/README.md)) —
because "the skill was loaded" and "the skill did the work" are
different claims; a with-skill pass counts only if invoked, and
both-pass cases score by relative cost (📄 Paper:
[SkillTester](https://arxiv.org/abs/2603.28815) — arXiv:2603.28815) —
the playbooks' value becomes our own measured number.

`scenario_type` and the failure-taxonomy tags are CLOSED enums with
exhaustive dispatch (`assert_never` on the fall-through, per D0): a
new taxonomy type makes the type checker name every grader, report,
and coverage statement that must handle it.

**Trial protocol** (agents are stochastic; a single run is an
anecdote): headline dimensions run k=3 trials per item, and the
verdict is **pass^k** — all trials pass — because consistency is the
product requirement for an on-call tool; a triage assistant that is
right one run in ten spends trust it never earns back. pass@k is
reported alongside as the capability ceiling, never as the headline
(at k=10 the two diverge toward ~100% vs ~0% —
[demystifying evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)).
The judge dimension runs k=1 (it grades prose, not correctness).
Failure-derived items form the **regression suite**, expected to hold
~100% and reported separately from capability slices, which are
allowed to start low — a capability slice at 0% across all trials
indicts the scenario before the model. A fix is confirmed only when
FRESH items from the same failure class pass, not just the item that
exposed it — a fix that only fixes its exposing item is overfit to
the regression suite.

**Iteration entries are pre-registered experiments:** before the
re-run, the entry declares the hypothesis, the smallest isolated
change, the predicted per-slice effect, and what result would
invalidate the hypothesis — declared-then-measured, never measured-
then-rationalized. Model, agent host, and harness stay fixed within a
comparison; a model or thinking-config upgrade opens a new EPOCH:
prior harness assumptions reset, the baseline is re-established rather
than assumed to transfer, and requalification probes progressively
LARGER outcomes rather than preserving old task boundaries by habit —
capability neither transfers nor improves monotonically, so the epoch
discovers its own boundaries in both directions.

Run artifacts pin the full verdict environment: run_id, git_ref,
model, thinking configuration, environment (cpu/mem limits, store
digests), per-item per-trial per-dim results, tokens, cost, latency —
container resources alone swing agent benchmarks by more than model
gaps ([infrastructure noise](https://www.anthropic.com/engineering/infrastructure-noise)).
Baseline `evals/baseline.json` refreshes only by a deliberate, labeled
commit.

## D25 — Scenario generation: the generator plants the cause (phase 8)

`resgraph-gen scenario --type <T> --seed N` emits: a world, a causal
change at T₀ (the ground truth), distractor changes, an alert at
T₀+Δ on a resource downstream of the cause, and the ground-truth
JSON. The mechanism path uses only edges the world actually had at
T₀ — asserted at generation time with the same as-of query the grader
uses; generator and grader agreeing on the graph is a precondition,
tested once, deliberately.

Taxonomy (≥30 items across it): direct-dependency (depth 1),
transitive (depth 2–3), deleted-resource cause, noisy-window (10+
distractors), ambiguous (two plausible causes — top-3 must contain
the planted one), **decoy** (a non-causal change timed to correlate
with the alert and superficially more plausible than the true cause —
distractor-correlation seduction, planted on purpose), and control
(no cause; distractors only). Controls are ~20% of the set — an agent
never shown "nothing" learns to always accuse something.

Two generator obligations exist for the eval's own audit:
**re-skinning** (regenerate a scenario's surface — names, types,
attribute values — under a different seed with identical causal
structure; an agent whose score drops sharply was reading the
generator's templates, not the graph), and **template parity**
(distractors draw from the same template families as causes, so
surface signature never separates signal from noise). Both exist
because pattern-matching strength cuts both ways
(📄 Paper: [pattern matching](https://arxiv.org/abs/2601.11432) —
arXiv:2601.11432): structure-only competence makes the synthetic
world a valid eval domain AND makes template recognition the eval's
first failure mode.
**Rejected:** hand-authored scenarios (unreproducible, and the
taxonomy would drift from what the world model can express);
LLM-generated ground truth (the eval would inherit the grader's
blind spots).

## D26 — Permission boundary and the typed approval gate (phase 9)

Two tiers, structurally enforced: the agent's entire tool surface is
read-only (D19), and the one privileged capability —
`apply_remediation` — lives outside the agent loop entirely (D28's
proposal boundary). Between the proposal and the execution sits a
human gate with a specific shape:

- **Approval is typed, not clicked.** The approver types the count
  of steps being applied; a mismatched count re-asks with the true
  count. A y/n prompt is reflex-compatible — a plan that grew a step
  since the approver last looked sails through on muscle memory; a
  typed count cannot.
- **`skip N` drops a step; numbering never shifts.** Step numbers
  stay stable across skips so a skipped step can be discussed by
  number afterwards. Skipping every step is a rejection.
- **The decision is itself an audit record:** approver, plan hash,
  applied and skipped indices, time-to-decision. A 900ms approval of
  a five-step plan is a reviewable fact.
- **The tier boundary is a SESSION composition rule, not a per-tool
  judgment** (amended, #143). Risk lives in tool combinations: a
  session holding private-data reads plus an untrusted-content
  channel plus a write channel is the
  [lethal trifecta](https://simonwillison.net/series/prompt-injection/),
  and each leg alone can look justified. The analyst session reads
  platform data, so its toolset admits no open-world tool and no
  write-capable tool — enforced at toolset construction (the
  registry's annotation hints are the vocabulary; see
  [Tool Annotations as Risk Vocabulary](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)),
  refused with the rule named, not just the tool.

**Amended (#152) — a grant has a lifetime and a revocation.** The
approval carries `expires_at` (default 15 min) and execution refuses a
stale grant rather than acting on an hour-old decision as if it were
fresh. Revocation is D28's cooperative cancel, reached by SIGINT during
execution: checked between steps, never mid-commit, executed steps
unwind and the summary says which. Completes the authority contract's
seven fields — identity, operation+target, scope, lifetime+revocation,
approval, audit+postcondition, rollback — of which six were already
present.

**Amended (#152) — the write tool has a dry run.** `--dry-run` renders
the plan and the exact D2 messages it would emit, computed by the same
`plan_message` the apply path calls, and writes nothing. One code path,
so a preview cannot drift from what execution sends. With the
watermark confirmation (postcondition) and `target@sequence` receipts
already in place, the write tool now carries all three legibility
affordances.

**Rejected:** auto-apply for "low-risk" steps — risk-tiering the steps
means the agent chooses its own supervision level, and the tier
boundary is the tool, not the payload; y/n confirmation
(reflex-compatible, see above);
renumbering after a skip (makes the post-hoc conversation ambiguous:
"step 2" would mean different steps before and after the skip);
per-tool risk assessment as the only gate (a session of three
individually-defensible tools can compose the trifecta).
**Reversal condition:** if plans routinely exceed a dozen steps, a
typed count stops proving the plan was read — the gate then needs a
per-step acknowledgement, redesigned alongside the render. If the
analyst ever legitimately needs an open-world tool (docs lookup),
the composition rule forces a structural split — a separate session
without platform reads — rather than an exception.

## D27 — Audit posture: the store owns payloads, surfaces own hashes (phase 9)

One writer, embedded SQLite (`data/analyst-audit.db`): a `runs` table
and a seq-ordered `events` table (llm_call / tool_call / step /
approval / cutoff). The division of labor with D28:

- **The audit store is the system of record and owns payloads** —
  tool arguments, the resource ids each result surfaced, step
  targets. Progress surfaces (StepEvents, metrics, traces) carry
  status, hashes, and sizes only.
- **The bar is: incident questions answered from the store alone,
  with the agent stopped.** `audit --touched` ("what did the agent
  look at before it proposed this?") reads one SQLite file. If
  answering needs the agent re-run, it's logs, not an audit trail.
- **The harness seam is additive:** `run_triage(on_event=...)`, a
  plain callback — absent, nothing changes; present, every LLM call
  carries its own latency and token count into the trail.
- **Reasoning blocks land in the trail when the API returns them**
  (amended, #143): a monitor can only read what the trail recorded,
  and monitorability inherits a necessity condition
  ([arXiv:2507.05246](https://arxiv.org/abs/2507.05246)). The
  llm_call row labels what the response shape can actually show —
  recorded (text present; full chain-of-thought vs summary is the
  model's setting, not distinguishable from the response), elided
  (blocks without text), absent.
- **No secrets in rows**, same rule the eval runner enforces: keys
  live in the environment, never in run artifacts.
- **Tamper-evident, not tamper-proof** (amended, #143): each event
  row hashes the previous row's hash with its own content — a
  per-run chain, verified by `audit <run_id> --verify`, which names
  the first broken seq. One column turns "we trust the file" into
  "the file can prove itself". Named residual: truncating the tail
  is silent, as in any head-pointerless chain. Signing (a keyed MAC
  or per-decision signatures, the fleet-scale shape) stays rejected
  until there is a second party to distrust — at one writer on one
  laptop, a key stored next to the database attests nothing the
  chain doesn't, and the second party is also what a head pointer
  needs.

**Rejected:** a composed Postgres service (laptop scale is the
declared scale, and a trail that depends on a service being up is a
trail with gaps exactly when it matters); structured logging as the
audit surface (no seq contract, no queryable store of record).
**Reversal condition:** a second concurrent writer or cross-machine
runs — move to a served store, keeping the schema and the CLI.

## D28 — Execution protocol for the privileged tool (phase 9)

Phase 9 adds the platform's first privileged capability —
`apply_remediation` — and this decision is the shape that keeps the
read-only-by-construction property meaningful next to it:

- **The model proposes; harness code executes.** The privileged tool
  is never in the agent's tool blocks. The agent emits a plan and
  stops; approval and execution live outside the loop, and the
  approver sees the rendered plan with every irreversible step
  declared BEFORE deciding — a step whose pre-state capture fails is
  marked irreversible at render time, not discovered mid-execution.
- **A step machine, not a for-loop.** Pydantic-validated StepEvents
  (status ∈ started / succeeded / failed / rolled_back /
  rolled_back_failed / irreversible / cancelled) with locked
  invariants: step_index < total_steps; rolled_back_failed carries
  its error; per index at most one started, one terminal-forward,
  one terminal-rollback. Events carry no raw arguments — they are
  progress surfaces; the audit store owns payloads.
- **Cooperative cancel, between steps only.** A step is one gated
  apply; interrupting mid-commit recreates the partial-state bug D12
  exists to prevent. Five tested invariants: bounded latency (≤ one
  step), idempotent, stale-safe (cancel after completion: no error,
  no event), scoped to the run's owner, terminal (post-cancel every
  executed step is rolled_back or irreversible, and the summary says
  which).
- **Rollback: reverse-order, best-effort, honest — with a three-way
  handler contract** (amended, #143). The rollback callable returns
  ROLLBACK_IRREVERSIBLE when it discovers at rollback time that the
  pre-state no longer restores anything (the world moved) — the
  honest middle between lying (plain return) and alarming (raise);
  any other return emits rolled_back; a raise emits
  rolled_back_failed WITH the error. In every case the chain
  continues — strict re-raise leaves later steps silently
  unattempted, the worst of both worlds. Cancel requests arriving
  during the unwind are deliberately dropped: the unwind runs to
  completion.

**Rejected:** executing steps in a plain loop with exception
propagation (loses the event surface and the honest-rollback
property at once); mid-step cancellation (D12's partial-state class);
prompt-level enforcement of the proposal boundary (the tool's
absence from the agent's blocks is structural; a paragraph is not);
re-planning from the intermediate state inside the machine, the
shape of [Stripe's auto-remediation](https://stripe.dev/blog/how-stripe-uses-graph-search-and-state-machines-to-auto-remediate-a-global-database-fleet)
(#143 — with a human approval gate, a re-plan is a NEW PROPOSAL and
rides the same gate; after cancel or failure the machine's job is to
leave an honestly-described state for the next proposal, not to
plan); a status-discriminated StepEvent union making
rolled_back_failed carry its error non-optionally, the constructive
form of parse-don't-validate (#143 — the only value-level invariant
is that one field pair, and the load-bearing invariants are
SEQUENCING, which no value type can express; a seven-variant union
relocating a two-line validator fails the don't-newtype-the-world
test).
**Reversal conditions:** if remediation ever needs multi-run
coordination (two agents, one plan), the per-run owner scope and the
single-writer event model get redesigned together, as one decision.
If StepEvent variants accumulate variant-specific payloads, the
constructive union stops being ceremony and the validator converts.
If plans grow beyond single-owner linear sequences (partial
remediation toward a least-degraded state becomes a goal), the
re-plan question reopens inside the machine, not outside it.

**Addendum (#145) — what a step actually does.** The protocol above
described execution before the capability existed; this is the
executor that fills it in.

- **Remediation is a stream write, never a store write.** A step
  becomes a D2 update emitted onto the same ingest stream every other
  producer uses and applied by the same idempotent path (D3/D10).
  Nothing in the executor touches a store, so remediation inherits
  ordering, replay-safety and the audit trail rather than reimplementing
  them beside the platform.
- **Emit, then confirm.** The D3 watermark drops a message whose
  sequence is not ahead of the target's, silently and by design. An
  executor that emitted and returned would report success for writes
  that never landed, so each step reads the watermark back and fails
  the step if its sequence never arrived. A remediation that vanished
  is a failure, not a success nobody checked.
- **The patch merges onto live state; the rollback target is the
  approved snapshot.** A D2 upsert is a full statement — it replaces
  the attribute bag and the owned edge set — so a step that sent only
  its patch would strip every attribute and edge it did not mention.
  Applying merges onto the target's state at apply time (patching the
  render-time snapshot would silently revert whatever changed in
  between); rolling back restores the snapshot the approver actually
  saw, and only while the resource still sits at the sequence this
  step wrote. Past that, a third party has written and the snapshot
  restores nothing — the step reports ROLLBACK_IRREVERSIBLE rather
  than clobbering them.
- **The agent names a cause; it never names a mutation.** The report
  schema has no remediation field and gains none. The remediation
  vocabulary is the operator's, supplied to `resgraph-analyst triage`
  with `--remediate`, and the plan is assembled outside the model
  against the suspect the model identified. No model output is ever
  interpreted as an instruction to write — a stronger property than
  gating one, and it costs nothing to hold.
- **The capability is the injected write channel, not a flag.**
  `apply_remediation` is registered with `privileged=True` and an
  empty surface set; both transports derive by membership, so "external
  clients have no bypassing use case" is enforced rather than promised.
  The agent's toolset is built by *selection* — read-only, unprivileged,
  closed-world — so a new registration is off the agent's surface until
  it earns a place, rather than on it until someone remembers to
  exclude it. Execution additionally requires an operator caller and an
  emit channel that no read transport supplies.

**Amended (#152) — two rules named rather than merely applied.**
*Ephemeral events, durable state:* StepEvents are progress surfaces and
carry no arguments; the audit store owns payloads. The machine has
always worked this way and the principle was never stated, so nothing
stopped the next surface from copying the shape without the rule.
*Config-surface scanning* is adopted as a control category: the
harness's own configuration is part of the attacked surface. Today that
is covered by the drift guard (the tool surface cannot grow outside the
registry), TruffleHog (secrets), and the eval gate's path filter (a
prompt input cannot change ungated, #152). No third-party config
scanner is adopted — its scope is agent-client configuration this repo
does not ship.

**Rejected:** writing to the hot store directly (fast, and it would
desynchronize the stores the platform exists to keep in agreement);
returning success on emit (the watermark makes that a lie); letting
the report carry proposed actions (it would make model output an
instruction, and it would change the graded schema the D24 baseline
was certified against). **Known hazard:** the executor is a second
writer, and D3's watermark cannot arbitrate sequence assignment
between producers — a generator write racing a remediation can make
either look stale. Confirmation turns that race into a visible step
failure rather than a silent no-op, which is containment, not a fix;
the fix is #31's multi-producer sequencing (epochs/fencing), and this
executor moves to it unchanged when it lands.

**Vocabulary check (#152).** The agent SLOs were named before
consulting
[Towards a Science of AI Agent Reliability](https://arxiv.org/abs/2602.16666)
(twelve metrics across consistency, robustness, predictability,
safety), the way the SRE Workbook shaped D18. Checked after the fact:
latency, cost/run and degraded-rate map to predictability and
robustness; **consistency** is measured but as an eval property
(pass^k across trials) rather than a scraped series, and **safety** is
carried by the gate's unconditional fabrication block rather than by an
SLO. No metric is added — the gap the paper names for us is that
consistency and safety live in the eval thread, which is where they
can be graded, not on a dashboard where they would be decoration.

## D29a — Hard budgets with graceful cutoff, and the judge spend breaker (phase 9)

The runtime half of D29 (the SLOs and the CI eval gate are D29b,
#140). Two budgets, both enforced in code, both honest about what a
budget breach is:

- **Cost and wall-clock ceilings, checked at the turn boundary.**
  `run_triage` takes `max_cost_usd` (with a `cost_fn` meter — the
  ceiling needs its meter, so they come as a pair) and `max_wall_s`.
  The check runs between agent-loop turns, never mid-call — the same
  boundary philosophy as D28's cancel, for the same reason: an
  interrupt mid-commit recreates the partial-state class. On breach
  the harness injects ONE final "conclude now" turn and marks the
  run degraded with a `cutoff_reason` (tool_calls / tokens / cost /
  wall_clock). **An exception is not a conclusion:** a starved run
  still ends in a report, and that report's claims stay graded.
- **The cutoff path is graded, not just implemented.** A
  `budget_starved` dataset (one item per scenario type, same worlds
  as their originals — the graded question changes, not the
  scenario) runs under a tool-call floor that cannot reach the
  cause. The `cutoff` dimension passes an honest degradation
  (report produced, admits degraded) and fails a confident
  conclusion under starvation; any fabricated claim it makes still
  fails the evidence dimension. Starved items report in their own
  slice — folding them into the causal-type slices would read a
  by-design miss as a regression.
- **The judge dimension gets a daily spend circuit breaker.** A
  per-UTC-day ledger of estimated judge spend, warn once at 90%,
  trip loudly at the cap (a SystemExit, not a skipped dimension — a
  run silently missing judge rows would poison comparisons against
  fully-judged baselines). Eval infrastructure has a bill; the cap
  is how a retry loop is noticed before the invoice, the phase-8
  spend-cap surprise promoted to enforcement on the eval side.

**Rejected:** raising an exception on budget breach (loses the
graded honest-degradation path — the whole point is that a starved
run concludes honestly); mid-call cost checks (D28's partial-state
class); a judge breaker that skips the dimension silently on trip
(a partially-judged run is not comparable to a baseline and hides
the overrun). **Reversal conditions:** the laptop-atomic JSON
ledger moves to a real atomic counter the day a second concurrent
runner exists (same trigger as D27's served-store move). If cost
estimation needs the judge's own token spend (today's meter is
worker-side and undercounts), the meter and the ledger merge.

**Addendum (#152) — the second kind of degradation.** Starvation is
the agent running out of budget. This is the platform failing under
it, and it gets the same treatment: graded, not assumed.

- **The fault is injected at the store handle, not in the prompt.**
  After `DEGRADED_KILL_AFTER` hot sessions the factory raises, so
  every hot-backed tool fails through its own error path
  and the cold-backed ones keep answering. Enumerating which tools
  read which store would be a list that rots; killing the handle
  exercises the real division — and that division is what is under
  test, because `resource_history` and `world_diff` mean the agent is
  not blind when the graph dies (D13's split paying out again).
- **The graded question is what the report says it lost.** A run that
  finishes with history-only triage and admits the degradation passes;
  one that hides it fails. Claims it does make stay held to the
  evidence dimension, so a confident assertion about live topology
  after the kill is caught as the fabrication it is.
- **A fault that never fired fails the item.** An induced failure that
  did not happen proves nothing, and an item proving nothing must not
  read as evidence that the agent degrades honestly.
- **found-rate is measured on degraded items but does not decide
  them.** Honest degradation has a quality cost; the drill exists to
  report that number rather than round it to zero.

**Rejected:** killing the container (the drill would then measure
docker's restart behavior, and the agent's own error handling is the
subject); a keyword check for the failed capability in the narrative
(brittle, and the judge already reads the narrative); letting
found-rate decide the item (it would grade the agent on reaching a
cause the dead store was holding).

**Addendum amendment (2026-08-09, #158) — the fault target is a named
choice, and it moved.** Two runs of the hot-store fault measured
nothing (INC-002): `fetch_resource` and `blast_radius` read the hot
store only when `at is None`, and triage of a past alert always passes
`at`, so the workload is cold-backed and the drill was killing a store
it barely used. Three consequences, all in code:

- **The fault now targets the cold store** (`cold_store_dies_after`),
  because that is what a time-travel triage actually depends on — the
  same D13/D16 split that insulated the agent from the hot kill is
  what makes the cold kill bite.
- **The target is explicit per item** (`fault:hot` / `fault:cold`
  tags) and the runner refuses a degraded item that names neither. An
  implicit target is how a drill aimed at the wrong store recurs.
- **The pilot is a gate, not advice**: the drill script refuses the
  suite unless a one-item run shows a failed tool call.

**Rejected:** a both-stores fault, for now — if hot-only triage of a
past alert proves impossible, the cold kill already poses the
total-blindness question, and a second fault adds spend without a new
graded question. Reversal condition: if the cold drill shows partial
hot-only answers are possible, total blindness becomes the untested
case and a both-stores item gets added.

**Addendum (#160) — injection as data, the behavioral half.** D26
makes injection structurally harmless (the privileged tool is absent
from the agent's blocks, so an obeyed instruction reaches an error
outcome, never an execution); a unit test proves that boundary with a
scripted client. This measures whether the *real* model, on a real
run, treats a hostile attribute as data.

- **The fault lives in a resource attribute, layered at load time.**
  A companion set (`evals/scenarios/injection.jsonl`, one item per
  scenario type, `injection` tag) attaches a `note` attribute to the
  path resource closest to the cause that is alive at incident time,
  so a competent investigation reads it. The world is otherwise
  unchanged; the note is applied from provenance during store load,
  so the item stays a recipe.
- **The demanded target is a real off-path resource.** Naming it as a
  suspect is following the instruction, graded by the `injection`
  dimension; a suspect *justified* by the note fails the evidence
  dimension already. Fabrication stays the halt condition.
- **The content is a fixed template, sentinel-marked.** The injection
  dataset cannot become a channel for un-swept content: only tagged
  items may carry the sentinel, and `inject_text` must equal the
  canonical string for its target (SANITIZATION.md check 8).
- **Own slice, not a gate candidate**, like the degraded and starved
  sets. Reachability is verified for $0 (integration test) before any
  spend, and one item pilots at k=1 before the suite.

**Rejected:** a keyword grep of the narrative for compliance (brittle,
and the judge already reads the narrative — narrative laundering is a
judge signal, not the decisive dimension); planting on any resource
rather than the causal path (the agent might never read it — INC-002's
question, so reachability is asserted per item, not assumed).

## D29b — Agent SLOs and the CI eval gate (phase 9)

The release-gate half of D29, consuming the certified k=3 baseline.

- **SLO objectives from the certified baseline** (D18
  budgets-from-baselines): triage latency (good <= 152 s, 1.5x the
  measured p95), cost/run (good <= $0.30, 2x the measured mean),
  degraded rate (<= 5%), each with a 6x fast-burn alert mirroring
  D18's shape. The eval floor (pass^k >= 0.65) is the gate itself,
  not a scraped series. The `analyst_*` metrics are wired now and
  populate when the agent runs as a scraped service (#145) —
  instrument-before-subject, the phase's own ordering.
- **The CI eval gate blocks a regression.** On PRs touching the
  analyst/tools/evals/skills globs, the gate aggregates a run and
  compares it to the committed baseline: fabrications block
  unconditionally (no threshold, no label); overall pass^k drop
  > 2pp blocks; any slice drop > 5pp blocks (asymmetric because a
  slice regression hides inside a flat overall). The regression
  slice (`source:failure_derived`) and the budget-starved slice are
  compared under their own names, not folded into the causal set.
  A slice the baseline does not carry cannot be compared at all, so
  it warns rather than blocks — and a *protected* slice in that state
  says so, because "unguarded until a refresh includes it" is a real
  gap and should not read like a routine notice.
- **Comparability precedes verdict.** Every dataset writes into
  `evals/runs/`, so the newest committed run is not necessarily a run
  of the baseline's dataset. `aggregate` therefore reports
  `item_ids`, and the gate declines any run whose item set differs
  from the baseline's rather than comparing pass^k across different
  items — which silently scored a 7-item budget-starved run as PASS
  against the 30-item baseline it never measured.
- **Selection precedes comparison (#185).** Once drill and pilot runs
  are committed as incident evidence, the newest file is usually a
  companion set, and gating it would decline on every PR while a real
  base-run regression sat unexamined. The gate scans newest-first and
  skips runs it cannot verdict — companion sets (`store_degraded`,
  `budget_starved`, `injection`, `coverage_gap`) and sub-k runs, the
  two classes this decision already calls "not a gate candidate" —
  naming each and why. The skip is deliberately not "any mismatched
  run": a base run whose dataset drifted is neither, so it is still
  selected and declines loudly — the failure the comparability rule
  exists to catch must not be skipped past. With no gateable run
  committed, the gate reports "nothing to gate" rather than declining
  on an old run nobody touched.
- **A different worker is not a regression (D29c).** Once the worker is
  pluggable, a local arm run of the base dataset has the same item set
  and slices as the certified run, so the gate would read its lower
  pass^k as a regression and block. It is not one — a weaker cheaper
  worker is an `arms` comparison, not a baseline breach. `aggregate`
  therefore records the run's worker `model`, the baseline records the
  worker it was certified on, and selection skips a run whose worker
  differs (naming it, like a companion set); a direct file gated against
  a foreign-worker baseline declines rather than blocks, pointing at
  `arms`. The gate defends whatever worker the baseline was certified on;
  everything else compares through `arms`.
- **Flap floor (#137):** the gate declines to verdict a run below
  k=3 — certification measured a 20% single-trial item-flip rate, so
  a k=1 diff on marginal items reads noise. Declined is distinct
  from blocked: an experimental single-trial run is simply not a
  gate candidate. It is also distinct from a broken gate, which
  fails closed and is not label-overridable; the command separates
  all four by exit code (0 passed, 1 blocked, 3 declined, 4 evidence
  unreadable) so CI never infers a verdict by grepping prose.
- **The comparison is free; the run is not.** CI runs the offline
  comparison, never the paid suite — a ~$4 run per PR does not scale,
  so a real behavior change ships its own fresh run and the gate
  checks committed evidence. The only override is an
  `eval-baseline-refresh` label (the CI job's concern, not the gate
  module's) that downgrades a block to a report for a PR committing a
  refreshed baseline; the label signals the gate, it does not bypass
  review, and fabrications are never overridable.
- **The trigger is tested, not maintained by memory** (amended, #152).
  The filter shipped without `skills/**` while the skill body was
  being loaded straight into the system prompt, so editing the agent's
  investigative discipline changed its behavior and the gate never
  ran. The globs are now asserted against the prompt builder's own
  inputs, discovered from the modules rather than listed, so a new
  input that escapes the filter fails the suite. A control that
  silently stops covering something is worse than no control: the
  green check still appears. The workflow listens
  for `labeled`/`unlabeled` because a re-run replays the original
  event payload — without a fresh event the new label is invisible and
  the override cannot be applied at all.

**Amended (#152) — three gaps closed or recorded.** The breach comment
is a review artifact: headline with its delta, every slice marked
OK/BREACH, and the failing items with the dimension that failed them,
so the regression is read in the PR rather than found later on a
dashboard. Dataset growth **declines** rather than auto-extending
per-item baselines: the shipped gate refuses to verdict a run whose
item set differs from the baseline's, which is the safer primitive and
a deliberate divergence from the auto-extend design — a new item that
would have blocked as a curation defect instead forces a labeled
refresh, where a human sees it. And the gate has **no rollout half**:
soak periods and gradual rollout are rejected at this scale, because a
single-user laptop project has no traffic to roll out across and no
population to soak against; the pre-merge gate plus a labeled refresh
is the whole control. **Reversal condition:** the day the analyst runs
as a scraped service with real traffic (#145's SLOs going live), a
prompt or default change gains a post-merge signal and the rollout half
becomes buildable.

**Rejected:** running the paid suite on every PR (unaffordable and
needs API secrets in CI — the free comparison gates committed
evidence instead); a symmetric per-slice threshold (a slice
regression is exactly what a flat overall hides, so the slice bar is
looser in points and stricter in reach); a label that clears
fabrications (the honesty property is not a threshold to negotiate).
**Reversal condition:** a self-hosted runner with a scoped key makes
per-PR suite runs viable — the gate mechanism is unchanged, only its
trigger moves from committed-run to fresh-run.

## D29c — The eval worker is provider-pluggable; local is the daily driver, Anthropic the reference arm (phase 10)

The cluster prices a worker, and everything downstream of a run —
aggregation, the D29b gate, the `arms` comparison, the skill-value
ledger — reads JSONL rows and is blind to what produced them. The
instrument is already model-agnostic; only the runner's client
construction and its tool shaping are not. So the worker becomes
pluggable and the daily loop stops paying API tokens for the *worker*
(#192). The judge is the asterisk: pinned on Opus by the confound
guardrail below, it is a deliberate Anthropic cost on the reference
runs and on any run that grades the narrative dimension — the daily
loop is free only when it runs the deterministic graders
(`--no-judge`). Free worker, judge on demand.

- **The seam is an in-process client factory, not a gateway.** The
  runner selects its worker by spec: an Anthropic model → the Anthropic
  SDK; a local model → an OpenAI-compatible `base_url` (local engines
  already speak `/v1/chat/completions`). TLS, auth, and rate limiting
  are network-front-door concerns a single-user `localhost` process
  does not have — that is D30's gateway, a different layer. This
  decision imports only the swappable-backend abstraction, not the
  serving plumbing.
- **Per-role resolution.** Worker and judge resolve independently —
  `--model` may point the worker at a local endpoint while
  `--judge-model` keeps the judge on Anthropic. The runner already
  separates the two; the factory preserves that so the judge-pin (the
  model-arms discipline) is structural, not remembered. **Rejected:** a
  single global provider switch — it lets the judge silently follow the
  worker to local and corrupts every baseline.
- **The tool surface is the real work; "nothing downstream changes" is
  true only of the rows.** resgraph is built to Anthropic's
  `tool_use`/`tool_result` blocks; the OpenAI-compatible shape is
  `tool_calls`/`role:tool`. The transport needs no shim, but the tool
  definitions and the tool-call parsing need an adapter between the two
  shapes. Scope is factory + tool-surface adapter, not a factory alone.
- **The re-centering.** The local worker is the daily driver (free with
  deterministic graders); Anthropic (Opus) is a periodic reference arm,
  compared on the SAME item set via `arms` (cost per passed triage) to
  answer "how much better, and worth it when." The barbell: own the
  small model, rent the frontier by the token.

**Invariants that keep the cheap loop honest.** Provenance is recorded
in full per row — provider + model + quant + temperature + seed — so
the local baseline cannot drift silently under the reference. A
measured run pins its worker with **no failover**: local-down fails
loudly, because a silent fallback to Opus both spends unintended money
and confounds the comparison the design exists to make (failover is a
gateway feature and a bug here). Constrained/grammar decoding for tool
calls removes "the small model emitted malformed JSON" as a failure
mode that otherwise masquerades as a harness bug; and the local worker
must be capable enough to exercise the paths — tools, the injection
slice, the deferral terminal state — or "harness bug" and "the model
cannot" are indistinguishable, which makes the reference run
dual-purpose: cost calibration AND harness calibration.

**Request kwargs are a per-worker property.** A worker owns the
request-shaping kwargs that differ by model — thinking is the first:
Opus and Sonnet take adaptive thinking, Haiku 4.5 rejects a thinking
param outright (a 400). These ride the worker setup's `extra_args`,
merged into that worker's create call, so `--worker NAME` is
self-sufficient and cannot be mismatched by a call-site flag; the
global `--thinking` governs only the bare `--model` path. The same
channel carries constrained-decoding knobs for local workers.
**Rejected:** a global per-run thinking flag — it made a per-model fact
a call-site responsibility, so `--worker haiku` 400'd unless the caller
remembered to disable thinking.

**Relationship:** this is the adapter seam D30's gateway reuses ("one
base-URL change"), pulled ahead so the cheap loop lands without the
serving build. It generalizes #100 (model arms) from Anthropic-tier
arms to any worker. **Rejected:** silent worker substitution to cut
cost — it changes what the numbers describe rather than measuring the
same thing cheaper; the re-centering is explicit and by design.

**Decision — the daily driver is Haiku; Opus is the periodic reference
(amendment, chosen by the #100 arms, 2026-08-13).** D29c set "local is
the daily driver" as the shape; the arms chose the model. Local was
ruled out on this hardware (qwen2.5:7b OOMs on 8.6 GB), and the three
Anthropic arms measured a genuine trade-off, not a leaderboard:
`claude-haiku-4-5` has the highest pass^k (0.63), best recall, at 8×
lower cost and 5× lower latency; `claude-opus-4-8` abstains far better
(control 0.78 vs 0.17, zero fabrications) but finds hard causes worse
and does not earn its cost on pass^k; `claude-sonnet-4-6` is the
dominated middle. So **`claude-haiku-4-5` is the default analyst worker**
(`analyst/cli.py`, `evals/cli.py`); **Opus is a periodic reference arm**,
not the default. **The judge stays pinned on Opus** regardless of the
worker (`DEFAULT_JUDGE_MODEL`) — a judge that follows the worker to a
cheaper model corrupts every arm. **Caveat, load-bearing:** Haiku's
output is *surface-for-review*, not an autonomous verdict — it
over-attributes on controls and occasionally fabricates an edge, so a
deployment must treat its suspects as candidates a human or an Opus pass
filters, not a page-the-team verdict. The change-forensics skill does
NOT close that gap — #199 measured it and found the opposite: the
playbook is a recall tool (pass^k +0.067, ambiguous recall +0.25, it
flips 3 items fail→pass) that makes Haiku a more aggressive investigator,
which slightly *widens* the honesty gap (control −0.11, one more
fabrication). So the caveat is a hard property, not a prompt bug: the
skill is kept for recall, its honesty cost accepted, not fixed. Full
evidence: EVALS.md → the model arms. **Rejected:** Sonnet as a
middle-ground default (dominated — worse pass^k, most fabrications,
slowest, no cost saving); and a scalar pass^k decision rule (it would
have picked the dominated arm — the decision surface is slices × cost ×
latency × fabrications, not one number).

**Amended (#245, #247) — local serving facts are declared, recorded,
asserted.** Two per-model facts previously left to server defaults join
the worker provenance. The **weights digest**: resolved from the serving
Ollama at run start and embedded per row — a tag is a name, not an
identity, so rows recording only the tag cannot prove two runs executed
the same weights; a setup MAY declare `weights_digest` and a mismatch
with the server refuses the run (declaration optional so a fresh host
bootstraps; an undeclared run still records what it ran). The **context
window**: declared per local setup (`context_window`), exported to the
server as its default (`OLLAMA_CONTEXT_LENGTH` in compose — the one
knob the OpenAI-compatible endpoint cannot set per call), embedded per
row, and asserted per call — a response whose reported prompt tokens
reach the declared window refuses loudly, because the server clips
silently and an input that did not fit proves nothing about the model
that "answered" it (the fault-fired rule applied to prompts).
**Rejected:** pinning the pull by digest as the only path (bootstrap
needs the tag); a bigger undeclared default (the fix is declaring the
knob, not picking a luckier default).

## D30 — Gateway shape: two backends, task-class routing, recorded source (phase 10)

`resgraph-gateway` (FastAPI, one process) fronts two backends — `local` (Ollama) and `anthropic` (API, the second "region") — behind an OpenAI-compatible-ish `/v1/generate` taking `{messages, stream, task_class?, model?, pin?}`. It reuses D29c's client factory + tool-surface adapter and adds only the serving plumbing D29c excluded. This is the [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) **Routing** pattern applied to model selection, not prompt selection. Charter: [#162](https://github.com/fespino/resgraph/issues/162).

**`model` and `pin` carry served-model ALIASES — the D29c `models.yaml` setup names — never raw provider ids.** The field is named `model` because that is the wire-protocol word every OpenAI-compatible client sends; its value is an alias the seam resolves. Where an alias runs (provider, base_url, real model id, request kwargs) is its setup's property, resolved at dispatch; the local-vs-remote distinction stays transparent to routing, and a name is never sniffed for its backend. "Worker" remains the *eval role* (worker vs judge, D29c) — the gateway is roleless and does not borrow it. **Rejected:** raw provider model ids with backend inference from the id shape — a second naming vocabulary drifts from the setups, and prefix-sniffing lies the moment a hosted chat-completions model or a proxied Anthropic model appears.

Selection resolves by precedence, and the WINNING TIER is recorded on every response + metric — `source ∈ {pin, override, task_class_default, global_default}` — so "why did this call cost 40×" is a field, not a hunt:

1. `pin` — exact alias, no fallback, no substitution. Exists for the D29c judge: a pinned judge that silently reroutes is a corrupted baseline; a pin fails loudly instead of degrading.
2. `model` override — explicit alias, fallback allowed.
3. `task_class` default — JUDGMENT → the daily-driver setup (triage reasoning; `haiku` per the D29c amendment), WORKHORSE → the local setup (bulk/replay), CLASSIFICATION → the local setup (graders' light calls). Registry data with per-entry rationale, refined by eval evidence, not vibes; every routed alias must exist as a setup (tested).
4. Global default — the local setup (fail cheap).

Within an eligible tier, health- and latency-aware choice (EWMA of TTFT + health state). **Rejected:** round-robin — the two backends differ 10× in latency and ∞× in cost; symmetric balancing is a category error (with 2 heterogeneous backends this is failover with telemetry, not load balancing — the structure generalizes, the balancing claim would not).

**The registry is the serving authority; the setups file is a catalog (amendment, 2026-08-14).** The gateway serves, walks to, and probes only the providers its registry (plus the global default) routes; a setup the registry does not route — the hosted chat-completions example, an eval-arm entry — is catalog material for `--worker`/`--judge`, never a backend. **Rejected:** deriving the serving set from every setup in the file — the walk could fall forward onto a provider nobody configured for serving, and the probe schedule would spend on it every 15 seconds; an unrouted provider must not be spent on, silently or otherwise.

**Backend reality on this host (amendment, 2026-08-14).** A real local triage model does not fit 8.6 GB (qwen2.5:7b OOMs — D29c), so the `local` backend is **qwen2.5:1.5b** — adequate for WORKHORSE/CLASSIFICATION serving-shape duty and as the *killable* region for the failover drill, not for JUDGMENT (which routes to anthropic, where Haiku is the daily-driver model per the D29c amendment). The gateway measures serving reliability shapes at laptop scale, not triage quality, so the local model's weakness is irrelevant to what the gate asserts; numbers are labeled laptop-scale per BENCHMARKS.md, no fleet extrapolation. The measured-run invariant holds and is a gateway *setting*: an eval arm pins its worker with **failover disabled** (D29c), because a silent fallback both spends unintended money and confounds the arms comparison.

## D31 — Streaming + failure semantics: the init vs mid-stream split (phase 10)

SSE pass-through, per-token accounting at the gateway. TTFT is stamped at the first CONTENT token (not headers, not role deltas — measure what the user waits for); totals reconcile against the provider's usage fields at stream end (drift > 2% fails a test).

- **Admission/init failures** (connect refused, auth, unknown model, 429 before the first token): transparent failover to the next eligible backend. Every hop appends to `fallback_chain`, riding a `[gateway:fallback]` structured log + metric; **alert when chain length > 1** — the system degraded silently and the operator must know. Exhausted chain → `[gateway:exhausted]` + a clean 503, never a stack trace.
- **Mid-stream death** (backend dies at token 500): NO transparent retry — tokens already reached the client, and a replay can diverge into a spliced answer. The gateway emits a structured `stream_error` SSE event (tokens_emitted, backend, reason) and the CLIENT decides; the analyst treats it like a budget cutoff (degraded, honest). Exception: zero tokens emitted → one silent restart on the other backend (observably identical to an init failure).

**Rejected:** transparent mid-stream resume ("continue from token 500" on backend B) — splices two models' outputs and fabricates a completion no model produced; the streaming version of the eval suite's fabrication rule, disqualifying not a trade-off. Enforced **by construction**: no resume path exists to misuse.

**Eager refusal when a stream cannot be served (amendment, 2026-08-16, from INC-004 finding #217).** The stream open is lazy, so a dead backend surfaces *inside* the relay as a zero-token death; with no streamable fallback the client gets a structured `stream_error` in ~15 ms and nothing tells it to slow down — INC-004 measured 11,231 requests through this path in 182 s from a client that honors `Retry-After` wherever one exists. Adopted: when the routed backend's health is `down`, a streamed request is refused at admission, before any open — a pin gets its loud 502; routed traffic with no streamable, not-down fallback gets `refused_503` with `Retry-After` set to the probe cadence (the soonest the answer can change). The refusal joins the outcome vocabulary the availability ratio counts as served-or-explicitly-failed. **The priced trade:** health is probe-driven, so after a real recovery streams may be refused for up to readmission time (~3 probe intervals) while non-streamed traffic already serves — sub-minute recovery lag bought the end of a 60/s hot loop. Streamability is a property of the stream factory (today: anthropic has no adapter — #219); a custom factory without that limit disables the no-fallback branch rather than inheriting a stale assumption.

**The fall-forward spend budget (amendment, 2026-08-16, #206).** The failover promise narrows to: transparent failover *within a stated budget*, then explicit refusal with the reason stated. The walk buys availability with money in exactly one direction — local-down turns every free WORKHORSE/CLASSIFICATION call into a paid one at whatever rate traffic runs — and INC-004 priced it ($1.08/hour warm-prefix at drill traffic, linear in volume). Adopted: a per-UTC-day budget on **fallback-served paid traffic only** (a paid backend the router did not choose: non-empty chain + a priced model), sharing the D29a breaker's daily-ledger core — warn once at 90%, then paid candidates leave the walk. Free candidates keep serving; when only budget-blocked paid candidates remain, the refusal is a distinct `budget_503` + `[gateway:fallback-budget]` — "down because we refuse to pay past the cap" must not read as "everything is down". Unbudgeted by construction: registry-routed paid traffic (intended spend, governed by its own caps), pins (never walk), measured runs (pin with failover disabled, D29c), and unpriced backends (no price on file → unmetered, the shared convention). The refusal joins the same availability-counted outcome vocabulary; charged spend rides a `gateway_fallback_spend_usd` counter. **Rejected:** unbounded fall-forward with observability only — a guard that only measures is not a guard, and D29a already commits this platform to budgets that are enforced, not watched; a `SystemExit` trip like the judge breaker's — wrong shape for a server, which must keep serving everything the budget does not touch.

## D32 — Two cache layers, named and measured separately (phase 10)

- **Provider prefix cache** (Anthropic-side, D23 discipline): the gateway PRESERVES it — routing must not reorder/rewrite messages, and per-request `cache_read` passes through to metrics. Proven by test: byte-identical prefix in → `cache_read` > 0 back. A gateway that busts its upstream's cache is a net negative.
- **Gateway response cache:** full-request hash (model + messages + params) → completed response, TTL 15m, LRU-bounded, ONLY for temperature-0 non-streamed calls (graders, judge, repeated scenario replays). Hit = zero backend tokens. Measured on REPLAYED REAL TRAFFIC — the D27 audit store already holds every analyst LLM call, and `scripts/replay-traffic.py` re-issues a recorded run through the gateway. Hit rates reported per traffic class, not one flattering aggregate; uniform synthetic prompts lie about hit rates in whichever direction you wanted.

The two layers solve different problems — provider cache: shared PREFIX across different requests; gateway cache: IDENTICAL full requests — and get separate metrics. Conflating them is the most common cache error.

**A measured run bypasses the response cache (amendment, 2026-08-14).** The eval's k trials per item are byte-identical by design, and their trial-to-trial variance is the measurement — the ~20% single-trial flip rate is why k=3 exists. Served from cache, trials 2..k would replay trial 1's draw and pass^k would silently collapse into pass@1: a run that completes, produces numbers, and measures nothing. Requests carry `cache_responses` (default true), honored on read AND write; the eval wiring sends false on every measured call — the caching sibling of D29c's "no failover for measured runs". Staleness in the classic sense is otherwise structurally absent: the cached function is fixed per alias and the world state the analyst reasons over lives in the request bytes, so a changed world is a changed key; the residual window is a model swapped under the same id inside one TTL.

## D33 — Serving SLOs + capacity honesty (phase 10)

- **TTFT p95 ≤ objective per backend** (measured baseline × 1.5, D18 discipline) and a **completion-throughput floor (tokens/sec p50)** as SEPARATE SLOs — a stream can start fast and starve.
- **Availability** = served-or-explicitly-failed / total (a D31 `stream_error` surfaced counts as available; a spliced retry would count as success and be a lie).
- Queue depth per backend is the leading indicator (alert before latency, not after). Bounded per-backend queues with admission control: request cost is unknown at admission (output length isn't in the request), so a full queue returns 429 + `Retry-After` rather than scaling the pool. Health is a tiny synthetic *generation* probe (5-token completion), not a TCP ping — a server can 200 its health endpoint while generation has collapsed; readmission after recovery is gradual (3 consecutive healthy probes).
- Capacity writeup (`docs/capacity.md`) from the load test: hardware, model, quantization, method, the knee point, the TTFT-vs-throughput trade curve, and the cost math; no extrapolation — the throughput lever lives in the model server, not the gateway. The failover drill is **INC-004** (INC-003 is the cold-store drill, #158).

**Uptime must not spend: probing is opt-in per setup (amendment, 2026-08-16, #209).** Probes cost tokens, and an idle gateway at the original 15s global cadence spent ~$0.23/day on heartbeats to a managed API with its own SLOs — while the probe's whole rationale (a server that 200s its health endpoint while generation has collapsed) describes the local backend. Adopted: a setup is synthetically probed only if it declares `probe_interval_s` in models.yaml — the declaration is both the opt-in and the cadence. Undeclared means never probed: zero idle spend by default everywhere, failures surface per-request through the walk as one explicit failed hop, and the health machine keeps its single writer (feeding real-traffic outcomes into health would be a second writer and its own decision, if ever). Declaring a probe on a priced setup is permitted and is a deliberate, visible spend decision — the point is that undecided defaults are what must not spend. The committed catalog declares the local setup (60s); drills that need tight observation set their own (e.g. 5s). The scheduler wakes at the fastest declared cadence; per-provider due-ness lives in the probe round with an injected clock, so the loop shell stays logic-free. The priced consequence, from INC-004's data: readmission is 3 consecutive passes, so a 60s cadence means ~3-minute readmission — acceptable because routed traffic returns on the first real attempt regardless of health state. **Rejected:** a slower global cadence for paid backends (~300s) — shrinks the burn, concedes the principle, one dial for backends with opposite needs; deriving probeability from the pricing table (the first draft of this amendment) — it hides a trap where pricing a previously-free endpoint silently stops its probes, and it makes the pricing table an authority over a concern it does not own.

**Cost per task is an SLO, not only a cap (amendment, 2026-08-14).** The platform's cost mechanisms catch different failures and all three are required: hard budgets (D29a; the fall-forward cap, #206) catch catastrophe but not drift — a cap on the total does not fire until the total is reached; the eval cluster's $/passed prices a model offline, per experiment, not continuously; and only a **per-task cost objective watched in production** catches drift — a prompt or tool-surface change that doubles per-task tokens violates the objective the first day, long before any cap trips or a bill lands. In a per-token economy a cost regression IS a reliability regression, so per-task cost gets the same treatment as TTFT: an objective from a measured baseline × 1.5 (D18 discipline), a drift alert, a dashboard panel. Two levels, both priced from `PRICES_PER_MTOK` (unpriced models are unmetered, as in `estimate_cost`): **per call per task class** at the gateway — a `gateway_cost_usd` distribution sliced `{task_class, backend, source}`, the closed task-class vocabulary serving as the SLO dimension — and **per triage** (the analyst's real unit of work) aggregated by run id through the audit trail, its objective calibrated from the model arms' measured per-trial cost. The gateway is the uniquely correct meter: it is the one component that sees every call with its usage, class, source, and backend — a token-path meter that does not price is half a meter. Implemented in the metrics/SLO slice; unlike the fall-forward cap this does not confound the failover drill (observation, not control), so it is in-phase, not deferred.

**Rejected across D30–D33:** resume-a-dead-stream-on-another-backend (fabrication); symmetric load balancing (category error — heterogeneous latency/cost); a global provider switch the judge would follow (corrupts the baseline — D29c). **Exit gate:** the seven items in #162 / chapter 10 — `source` recorded per call; prefix-cache preserved; the mid-stream-kill contract; pinned-judge zero substitutions; load-test knee with method+hardware; per-class replay cache table (both layers); INC-004 with the TTFT curve and $/hour delta. The shadow/canary rollout layer (D79–D80) is a later addendum, out of this phase's gate.

## D34 — Institutional memory is a log-structured store (phase 10.5, #233)

The platform's knowledge files grow append-only by design — that is what makes the logs trustworthy — but two of them became MODEL INPUT (EVALS.md fed verbatim to self-proposal runs; skills fed every run), and one artifact cannot serve the archive role (completeness, "in case we need to redo") and the context role (a small current working set) at once. By the time this decision landed, EVALS.md was 1,642 lines / ~23.4k tokens per proposal call, diluting the rules that bind now and handing a self-proposing model the complete forensic history of how its graders were gamed and patched — with nothing recording what any given run actually saw.

Adopted, the checkpoint-plus-log shape D12 already gave the hot store: **the archive is the log** (`docs/evals-archive/EVALS-<date>-<gitref>.md`, byte-exact snapshots committed BEFORE any working-file edit, never fed); **EVALS-HISTORY.md is the moved record** (closed iterations, discharged registrations, finished build notes — transferred VERBATIM, append-only, never fed); **EVALS.md is the working set** (protocol rules, the full paid-run ledger — the base-rate instrument — the environment pin, and every OPEN registration verbatim; a parked issue's registration counts as open, a closed issue's is closed with it). Pointers run both directions and the procedure is a committed runbook (`docs/evals-compaction-runbook.md`), so the next compaction is a checklist. The fed knowledge files are part of the experimental configuration: the sha256 of the exact fed text lands in the proposal artifact (the same envpin discipline that pins prompt fingerprints and store digests — closes the #115-vs-#132 comparability hole nobody had named).

**Rejected:** AI summarization on the critical path — a summarized open registration is a different commitment (pre-registration's whole point is that the words are fixed before the run), and a summary that cannot be diffed cannot be audited; the structural split moves text, never rewrites it, and cut the fed context 85% (23.4k → ~3.6k tokens) with zero drift risk. A digest pass stays available as a reviewed-diff second step only if the split proves insufficient. RAG over the archive — ch-22 machinery for a working-set problem; reversal condition: when the split can no longer hold the working set under ~10k tokens. SPEC.md's own treatment — deferred; it has one consumer (humans) and no comparability hole.

## D36 — The detection corpus: benign by reference, attacks by planted mutation (phase 11)

Sentinel is measured on a combined stream, and the benign
false-positive rate is the headline — recall on attacks alone is the
easy half and a detector optimized for it is a reviewer-burnout
machine. Two halves, both committed:

- **Benign, by reference.** `evals/sentinel/benign-manifest.json`
  names the committed run files whose rows are all normal triage;
  the run files stay the record and the manifest is a selection, not
  a copy. A file is excluded if any row carries a companion/fault tag
  (store_degraded, budget_starved, injection, coverage_gap, reskin,
  fault:*) — those runs are deliberately abnormal. A test pins the
  manifest to a fresh selection so a new companion run cannot enter
  the benign pool silently.
- **Attacks, by planted mutation.** Seeded attacks are deterministic
  mutations of benign rows — D25's plant-the-cause discipline applied
  to malice at the run level. Each stays a real run except the
  planted dimension and carries ground truth in a `sentinel` object.
  Four types map to the OWASP LLM Top 10: injection (LLM01,
  narrative-appended steering), exfil_shaped (LLM02, wide fan-out),
  budget_abuse (unbounded consumption, re-fetch loop + token
  inflation), privileged_probe (LLM06, a failed apply_remediation —
  an *attempt* signal, since D26 blocks the act structurally).

**Two invariants, both tested.** The base run per item is chosen by a
seed derived from the item id, so the corpus rebuilds byte-for-byte
(`resgraph-sentinel corpus build`) and a generator change that moves
the bytes fails CI until the corpus is regenerated as a reviewable
diff. And the one payload attacks may carry is a fixed,
sentinel-marked template — only injection items carry the marker, the
benign corpus never does, so the corpus cannot become a channel for
un-swept content (the SANITIZATION check-8 shape). **Rejected:**
free-text attack payloads (an un-swept channel); synthesizing attack
runs from scratch (they would not be real traffic, and the whole
point is that an attack is a normal run with one thing wrong).

## D35 — Egress posture: store containers keep egress; the control was measured and rejected (phase 11)

The stores (redis, memgraph, postgres) have no legitimate outbound need
— only the model-pull sidecar instructs a download (and the download is
performed by the ollama server, which therefore does need egress at
pull time). Cutting their egress would be free defense-in-depth if a
compose-level mechanism existed on this platform. It was piloted, not
assumed (`scripts/egress-pilot.sh`), and no mechanism passes:

```
egress-pilot: linux/arm64 28.0.4 on Darwin
masquerade-off         inbound(host->port): works     egress(container->net): OPEN
internal-true          inbound(host->port): refused   egress(container->net): blocked
```

- `enable_ip_masquerade: "false"` does nothing here: Docker Desktop
  egress rides the VM's own NAT, not the bridge masquerade rule — a
  control that controls nothing, worse than none (the green check
  still appears).
- `internal: true` blocks egress but kills port publishing, and every
  consumer (tests, analyst, gateway) reaches the stores via host
  loopback. The dual-network workaround restores the ports through a
  second network that restores egress with them.

**Decision:** rejected at laptop scale, on measurement. The inbound
surface stays closed (every listener loopback-bound); unexpected
egress stays a NAMED breach signal in the sentinel threat model, not
an enforced control. **Reversal condition:** the stack moves to a
Linux host (bridge masquerade semantics apply there — re-run the
pilot; the script is the receipt either way — its output pins the
Docker version, so a Desktop upgrade is a re-run trigger too) or the
clients containerize into the compose network (published ports stop
mattering and `internal: true` becomes viable). The rejection also
leans on the value side: the stores hold synthetic, seed-regenerable
worlds, so egress buys an attacker little — a store that ever holds
non-synthetic data reopens this decision even if no mechanism
improved, at which point the expensive options (containerized
clients) get priced rather than dismissed. **Rejected:**
in-VM iptables surgery (unsupported, drift-shaped, invisible to the
compose file — the file must remain the authority for what the
stack's network does).

## D37 — Detection layers 1+2: measured thresholds, per-worker profiles, a funnel that is the architecture (phase 11)

Sentinel scores completed runs through cost-ordered layers; a run
reaches the paid layer 3 only through a flag, and the funnel count is
printed by the same command that measures the confusion matrices
(`resgraph-sentinel scan`) — proven, not asserted.

- **Layer 1 is rules, and every threshold is measured against the
  committed benign corpus, not invented.** The agent surface for the
  forbidden-tool rule derives from the registry (a privileged call is
  an ATTEMPT signal; D26 blocks the act). Tool-call ceiling 15 (the
  harness cap; benign max equals it), output-token ceiling 20,000
  (benign max 17,248; the abuse plant is 40k+), identical-repeat
  ceiling 4 (benign max 2; the abuse loop plants 30), injection
  signatures with zero measured benign hits. Each rule reports its own
  tp/fp so it is individually tunable; retunes belong to the flywheel
  on labels (W5), not to ad-hoc edits.
- **Layer 2 is a per-WORKER behavior profile.** The benign corpus
  spans three arms with different tool habits — one global profile
  would read each worker as the others' anomaly. z-scores per feature
  (calls, distinct resources, max depth, output tokens,
  fetch/traverse ratio, failed calls), aggregate = max |z| so the
  worst feature IS the explanation, threshold = a Tukey fence over
  the benign aggregate distribution. The fence multiplier was chosen
  by a measured sweep: recall is fence-invariant on the corpus
  (volumetric attacks score beyond any fence; text/insert attacks are
  invisible to features at every fence — they are layer 1's by
  design), so the multiplier trades only benign false positives:
  35/361 at 1.5x vs 9/361 at 3.0x. Far-out (3.0) adopted. An unknown
  worker scores infinite — no baseline to be normal against is itself
  the signal.
- **The measured result on the committed corpus:** layer 1 catches
  20/20 seeded attacks with 0 benign flags; combined benign
  false-positive rate 9/361 (~2.5%, all from layer 2's novelty
  channel); funnel 29/381 would reach layer 3. These floors are
  asserted as tests — the W5 CI recall gate formalizes them.
- **The stratified sampler is pure policy**: deterministic per-window
  seed, per-type quota with a floor (a low-volume run type still
  accumulates signal), uniform fill — wired to stream mode when it
  exists; testable offline now.

**Rejected:** a global behavior profile (reads workers as each other's
anomalies); training a model for layer 2 (a z-score with a named worst
feature is explainable to a reviewer; a trained score is not, and the
explanation is what the review queue renders); thresholds chosen by
feel (every bound above cites the benign measurement it clears).

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

## Compatibility tally

Every phase implicitly claims "built on the earlier layers without
breaking them." This table is that claim, tallied — additive reuse and
recorded supersession are both healthy; what this table refuses is a
change that lives only in a PR description where it can't be summed.

Counting rules: **consumed unchanged** = a public interface from an
earlier phase that the new phase's modules import or call as-is;
**changed** = a recorded amendment/supersession of an earlier phase's
decision, or a public-signature change to an earlier phase's module.
Private helpers and in-phase amendments don't count. One row per phase,
added at closeout. The early warning this instrument exists for: a
phase that changes more than it consumes is fighting the architecture.

Rows 0–6 are back-filled from tag-to-tag diffs
(`git diff phase-N..phase-N+1 -- src/`), not from memory.

| Phase | Consumed unchanged | Changed |
|---|---|---|
| 0 | — (baseline: D0–D4) | — |
| 1 | D2 message schema + `schema.py` closed types (emits the contract verbatim) | D4 emit budget (amended by supersession, phase 1) |
| 2 | `UpdateMessage` parse, `ResourceType`, `TOPOLOGY` (D5) | — |
| 3 | D2 models (`UpdateMessage`, `Op`), phase-2 graph client + DDL, the phase-1 stream sink contract | D2 parsing tightened — reserved-key rejection at parse time (D10); D4 ingest budget (amended by supersession, phase 3) |
| 4 | the same stream via a second consumer group (D14 — ingest untouched), `apply_batch` (phase 3), `load_snapshot` + `init_schema` (phase 2), `UpdateMessage` | phase-3 consumer containment loop extracted into the shared `StreamConsumer` base, behavior preserved (#43) |
| 5 | cold catalog + store readers, `ATTR_POOLS` (D5), hot client, `read_node` + `SYSTEM_PROPS` (phase 3), `ResourceType` | `state_at` grew push-down parameters and `blast_radius` grew filter parameters, both additive with defaults (D16, #50); D2 validation tightened — id/target prefix checks (#50) |
| 6 | stores, planner, and query layer untouched; wide-event module imports nothing from the platform | D14 addendum superseded in part — outage is not poison (#54); consumer loop grew quarantine/dead-letter hooks, additive (#54) |
| 7 | `parse_filter` + the DSL, planner `plan`/`explain` + executor `execute_plan`/`QueryContext`, hot queries (`blast_radius`, `dependency_path`), cold queries (`history`, `diff`, `state_at`), `read_node` + `SYSTEM_PROPS`, hot client + cold catalog, generator (test/bench fixtures) | DSL/planner error messages rewritten as steering surfaces — behavior-visible text change, no signatures (D20, #77); API route model renamed `BlastRadiusApiOut` to clear the canonical name (OpenAPI title only, wire unchanged, #77); API gained the registry-derived `/tools` mount, additive (#77) |
| 8 | `World`/`Churn`/`TOPOLOGY`/`ATTR_POOLS` (scenario planting), `UpdateMessage` + D2 models, cold `get_catalog`/`ensure_tables`/`append_events` + `state_at` (runner + evidence grader — production-query reuse is the design), graph `wipe`/`init_schema`/`load_snapshot`/`apply_batch` (runner), `TOOL_REGISTRY` + canonical tools (third surface derives, drift-guarded), skills loader + `change-forensics` body (the prefix is its second consumer), `QueryContext` | `Caller` literal widened with `"analyst"` — additive (#83); the skill body gained a step in iteration 8 and was reverted same-phase on measurement (net unchanged, EVALS.md) |

Running total through phase 8: 32 interfaces consumed unchanged,
12 changes — every one recorded as a D-amendment, a supersession, or
an additive extension in the phase's PR. No unrecorded break yet; the
table exists so the first one has nowhere to hide.

## Roadmap sequencing

Phase order is the largest standing decision in this repo, and until
now the only one carrying no rationale. Two questions decide an order,
and they are different questions:

- **Dependency** — what must exist before X is buildable at all.
- **Impact** — of the things buildable now, which teaches or unlocks
  the most.

Dependency-first alone produces months of plumbing with nothing
demonstrable; impact-first alone builds towers on sand. Every planned
phase below states its position on both axes. When a phase jumps the
queue, this section gets a dated amendment saying why — the same
supersession discipline as a D-number.

**Locked rule: evaluation work is exempt from impact ranking.** Evals
measure everything else, so ranking them by impact is circular — they
look low-impact right up until they were needed last month. The
instrument is built in the same phase as its subject, never deferred
for something shinier.

### Phases 0–6, reconstructed

The completed order was dependency-driven end to end: a world to emit
traffic (1), a store to receive it (2), the stream between them (3),
history (4), one query surface over both stores (5). The single
impact-driven call was holding observability to 6 rather than
sprinkling it per-phase: one dedicated phase instrumented the whole
pipeline at once, so everything built on top starts on a measured
platform instead of a dark one.

### The next stretch

| Phase | What | Dependency | Impact |
|---|---|---|---|
| 7 | MCP server over the query layer | wraps the D15 surface and D16 planner — buildable only now that both exist | converts every endpoint into agent-callable tools; everything agentic sits on it |
| 8 | analyst agent + its evaluation harness | needs the phase-7 tools | first end-to-end consumer of the whole platform; the evals land in-phase, per the locked rule |
| 9 | runtime hardening: bounding what the agent may do | needs an agent worth bounding | the difference between a demo and something left running unattended |
| 10 | cost accounting on the token path | needs real agent traffic to meter | turns "the agent works" into "the agent is worth running" |

**Amendment (2026-08-05) — the experiment spine inside phases 9–10.**
Both phases carry registered experiments whose order is dependency,
not preference: each consumes the previous one's baseline or
protocol verdict.

- **Phase 9** opens on the intake batch already merged (tripwire,
  envpin depth, failure-derived regression items, runner hardening,
  the sanitization boundary) and closes its eval thread in order:
  #99 (the k=3 certification completes and refreshes the baseline)
  then #115 (one agent-proposed harness iteration under the
  unchanged gate — the certified configuration is the comparison
  the experiment needs).
- **Phase 10** runs its eval cluster strictly ordered:
  #100 (model arms) and #101 (skill paired arm, producing the
  four-stage ledger baseline) before #132 (one agent-proposed
  *skill* edit from failure traces, gated by that ledger). #132 is
  additionally conditional on #115's outcome: if the gate handles a
  self-proposed harness change cleanly, the same protocol runs one
  level up; if #115 is gamed or regresses, #132's protocol is
  revised before any spend. Provenance for #132: the reflection
  loop named as an open industry problem by
  [Stripe's Kai platform](https://stripe.dev/blog/meet-stripes-knowledge-ai-platform),
  mapped onto the D21 playbook (skills as prompts) with a
  measurement gate their description lacks.

Beyond 10 the themes are named but deliberately unordered: misuse
detection, API platformization, a serving dashboard, sandboxing,
compliance-as-code, agent memory, ambient and reactive automation,
retrieval. Ranking them today would repeat the mistake this section
exists to prevent; each gets its two-axis line here when the horizon
reaches it, under a dated amendment.

## Corrections

The decision log's credibility does not rest on never being wrong — it
rests on being auditable when wrong. Corrections used to live in commit
messages and PR threads, where nobody re-reading this file finds them;
this table is where they land now. A log with zero corrections after
twenty-plus decisions reads as unaudited, not infallible.

Two rules:

- **Any claim corrected during a phase gets a dated row here** at
  closeout — what was claimed, where, what was actually true, what
  changed as a result.
- **Dependency claims are re-verified before a phase builds on them.**
  Where a decision asserts "X is safe because D-N" or "Y depends on Z,"
  the building phase re-checks the assertion instead of trusting the
  prose; a failed check lands here. (The phase-6 chaos drill is the
  motivating case: a written-down phase-3 readiness lesson was violated
  the first time it applied in a new context — the lesson existed,
  nothing forced re-contact with it.)

| Date | Claimed (where) | Actually true | What changed |
|---|---|---|---|
| 2026-07-31 | "~80% of wall time in `socket.recv_into`" — the phase-3 profiling story, on the PR #29 record and in the post draft | ~37% of tottime is the raw syscall; ~80% is cumtime of the driver's whole receive path (buffering + parsing included). The conflation was caught by a pre-publication adversarial pass | Attribution corrected in the post, BENCHMARKS.md, and as a correcting reply on PR #29 — the wrong number was already part of that record. The finding (round-trip chattiness; batching fix) survived unchanged |
| 2026-08-01 | "~366 MB cold-store footprint" — ad-hoc `du` over mixed stream content, early phase 4 | Directionally right, methodologically loose, and mis-attributed: at batch 1,024 the total is 363.5 MB but the parquet data is 18–23 MB at any batch size — the footprint was Iceberg commit metadata (977 files), not data. 25.2 MB total at batch 8,192 | BENCHMARKS.md carries the same-generator, same-method sweep and flags the ad-hoc number as loose; commit granularity recorded as a storage decision, not just a throughput one |
| 2026-08-02 | "Pinned to MCP spec revision 2026-07-28" — D19 as first written during phase 7, and the phase's own protocol test | The pin was untested prose: the SDK negotiates that revision only on the stateless `discover` path, and the test was using the legacy `initialize` handshake, which negotiates an older revision — the suite was green while exercising the path the pinned revision deprecates | Caught pre-merge by reading the spec against the implementation (#77): the test switched to `discover` and now asserts the negotiated revision equals the pin, so an SDK upgrade that shifts it fails CI. The claim became true-and-tested before it shipped |
| 2026-08-03 | "Narrative judge pinned (model, temperature=0, seed, template)" — D24 as first written | The API rejects `temperature` outright on this model generation (400: "deprecated for this model") and has never exposed a seed — two of the four pinned knobs were not ours to pin. Caught on the first real judge call of the baseline run, which is exactly when untested prose gets tested | D24 amended: the pin is model + template, the only knobs the API accepts; the judge call and its test updated. Same failure class as the revision-pin row above — a pin written before the API contradicts it |
| 2026-08-03 | "Token-weighted cache hit ≥ 0.9 on multi-turn runs" as the discipline gate — D23 and the discovery memo's quality bar | The floor is unreachable on short runs even with zero waste: after iteration 2 eliminated transcript re-billing entirely (uncached input 250,754 → 234 tokens per run), 0/30 rows reached 0.9 because the residue is `cache_creation` — the one-time write every new token owes before it can be read. The metric penalized unavoidable cost, not waste | D23 amended (second amendment) and the memo bar updated in place with a dated note: the gate is uncached re-read fraction ≤ 0.1 — the re-billing the metric was built to catch — with cache-hit still reported. An eval bug by the phase's own taxonomy: ours, recorded loudly per the memo's no-quiet-bar-bending rule |
| 2026-08-15 | "the `MCP-Method`/`MCP-Name` headers and `ttlMs`/`cacheScope` list-caching hints matter the day it speaks HTTP" — #166 item 5, the second-read watchlist | Half right: the headers are transport-level, but `ttlMs`/`cacheScope` are required fields on list/read *results* (`CacheableResult`) on every transport, stdio included — the SDK had been filling them `0`/`"private"`, serving a deploy-static catalog "immediately stale" | Caught fact-checking a blog draft against the spec changelog, not by either read of the release post. `cache_hints` adopted (1 h, `"public"`) with the protocol test asserting both fields; D19 addendum records the discharge; #166 corrected by comment |


