---
date: 2026-08-01
categories:
  - Data platform
tags:
  - iceberg
  - lakehouse
  - time-travel
  - benchmarks
  - disaster-recovery
---

# Cold history: time travel runs on event time, not commit time

The hot store answers "what is affected *now*." Nothing, until this
phase, could answer "what was true at 14:03 last Tuesday" — and the
obvious shortcut is a trap. Iceberg ships with time travel built in,
and it is the wrong clock for the question: it answers *what had been
ingested when*, not *what was true in the world when*. The two drift
apart the first time you replay, backfill, or lag. This post is about
building the right clock on top of the wrong one — plus a benchmark
where the previous phase's tuning advice inverts exactly, and a
disaster-recovery drill that failed in a way that taught me more than
passing would have.

<!-- more -->

This is the sixth post about **resgraph**, a mini referential data
platform built in public. The pipeline so far: a deterministic
generator streams infrastructure updates, a consumer applies them
idempotently into a graph store, traversals answer blast-radius
questions about the present. This phase adds memory: the same stream
lands in Apache Iceberg tables as durable history, and a query layer
reconstructs the world as of any moment. It also pays a debt — two
phases ago, tombstone garbage collection was deferred with the note
"the cold store holds history." Now it actually does.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-4-cold-store`](https://github.com/fespino/resgraph/tree/phase-4-cold-store).

## The shape: a log, and state as a view over it

The design is deliberately old-fashioned. One append-only `events`
table holds every message the stream ever carried. A second table
holds periodic **state snapshots** — but derived purely from the
events table, never from the hot store, so the cold half can answer
alone. Reconstructing "the world at T" is a checkpoint-plus-log read:
take the newest snapshot at or before T, replay the events above its
watermark, let the highest sequence per resource win, treat deletes as
absence. That is the same shape the hot store's own recovery uses, and
the same shape databases have always been underneath — the log is the
truth, everything else is a view that can be rebuilt from it. The
canonical argument for that sentence is Jay Kreps'
["The Log"](https://www.linkedin.com/blog/engineering/distributed-systems/log-what-every-software-engineer-should-know-about-real-time-datas-unifying);
this phase is a small working proof of it, with one deviation worth
naming: where Kafka unifies the subscribable log and the durable log,
this design splits them — the stream transports, the events table
remembers — which is why new consumers here bootstrap from the cold
store and then tail the stream, rather than replaying a topic from
offset zero.

Delivery is at-least-once and the writer stays deliberately dumb:
duplicate appends are legal, and readers dedupe on
`(resource_id, sequence)` with one window function. A crash between
append and acknowledgment produces duplicate *rows* and identical
*answers* — the integration test asserts both halves of that sentence,
because the second without the first would be testing nothing.

The whole stack runs in-process: pyiceberg with a SQLite-backed
catalog, a filesystem warehouse, DuckDB reading Arrow scans. Zero new
containers — the laptop shape of what would be S3 plus a REST catalog
in production, with the table format and its semantics carrying over
unchanged.

## Two clocks

Iceberg's native time travel lets you query any table as of a past
snapshot. It is genuinely useful — physical isolation, rollback,
debugging what a job saw — and it is deliberately **not** this
platform's as-of API, because Iceberg snapshots are stamped in
*commit time*: the moment the ingest ran. The question users ask is in
*event time*: the moment the world changed.

While ingestion is perfectly live the two clocks agree, which is
exactly what makes the confusion dangerous. Replay a stream after an
outage and yesterday's world changes arrive in today's commits;
commit-time travel now assigns a day of history to the wrong moment.
The event-time answer stays correct throughout, because it comes from
the data — every row carries the world's own timestamp — rather than
from ingestion bookkeeping. The decision (D13 in the spec) locks that
in: Iceberg snapshots remain physical machinery; questions about the
past are answered from event time, always.

## The benchmark: last phase's advice, inverted

The hot-store phase measured batch size 1,024 as its throughput sweet
spot, with 2,048 already regressing. The cold path is the mirror
image:

| Measure | N | Batch | Result |
|---|---|---|---|
| append | 200k | 1,024 | 24.3k events/s |
| append | 200k | 8,192 | **194k events/s** |
| append, sustained | 1M | 8,192 | **136.5k events/s** |
| `state_at` p50, snapshots on | 1M | — | 0.17–0.39 s |
| storage, data / total | 1M | 8,192 | 18 MB / 25 MB |

Batch 1,024 costs **eight times** the throughput of 8,192, because
every batch is an Iceberg commit — a manifest write and an atomic
metadata swap — and commit overhead dominates small appends. Same
stream, two sinks, opposite optima: the number that tuned the hot
consumer would have crippled the cold one, which is the concrete
argument for per-sink batch knobs over shared constants.

The second finding hides in the storage column. The first 1M-event
ingest ran at the hot consumer's default batching and left **366 MB**
on disk. The same events at batch 8,192: **25 MB**. The data was never
the problem — eighteen megabytes of parquet either way. The other ~340
MB was Iceberg *metadata*, compounding across a thousand small
commits. Commit granularity turned out to be a storage decision, not
just a latency one.

The footnote to the remedy: expiring old snapshots (every
commit is one) pruned the metadata log from 196 entries to 1 and freed
**zero** disk. Expiry shrinks what the query planner carries; deleting
orphaned files and compacting small ones is engine territory
(Spark/Trino), and the maintenance command's output states that
instead of implying otherwise.

For the first time in this project, every provisional budget survived
its measurement — append 13× over target, as-of latency 5×, storage
20×. After two phases of missed budgets amended by supersession, that
much headroom reads less like triumph and more like overcorrection:
the budgets were set timidly. Both directions of miscalibration are
now in the log.

## The drill that failed correctly

If the log is the truth and the hot store is a view, then destroying
the hot store should be a recoverable event — and that claim is only
real if you run the drill. `resgraph rebuild` reconstructs the graph
from cold history: world state at T becomes synthesized upserts
through the bulk loader, each carrying the resource's original
sequence, so every idempotency watermark survives the rebuild and a
resumed stream consumer skips replayed history instead of re-applying
it.

The first version of the drill test failed, and the failure is the
best thing in this phase. Rebuilding from *alive* state alone restores
watermarks only for the living. A resource deleted before T has no
node in the rebuilt store, no watermark — and when the resumed stream
redelivers one of its old upserts, the dead resource quietly comes
back to life. Resurrection by replay: state looks right, counts look
right, and a zombie walks the graph.

The fix reads the dead from the log too: a query for every resource
whose final event at T was a delete, re-applied as tombstones through
the normal ingest path so their watermarks are restored alongside the
living's. One subtlety made the query interesting — it must *not* use
the snapshot acceleration, because snapshots record only alive
resources; a delete below the snapshot's watermark would be invisible
to the fast path. The drill now proves the full loop: wipe the store,
rebuild, verify state and watermarks against the generator's oracle,
attempt to resurrect a dead resource (refused), resume the stream
tail, land on the same world.

## What I'd take to the next project

- **Ask which clock a "time travel" feature runs on.** Commit time and
  event time agree right up until replay, backfill, or lag — the
  moments you need history most are the moments they diverge.
- **Batch tuning does not transfer between sinks.** Measure each sink's
  knee; the previous component's sweet spot can be this one's worst
  case, and at small commits the cost shows up on disk as well as in
  throughput.
- **Run the destruction drill.** "The store is a rebuildable
  projection" was true-sounding prose until the drill failed on
  resurrected tombstones. The gap was invisible to review and obvious
  to the test — drills find what reviews don't.
- **State-only backups lose the dead.** Anything rebuilt from a
  snapshot of the living needs a separate answer for deletions, or
  replay will resurrect them. This generalizes well beyond graphs.

The platform now remembers. Ahead in the series: publishing this blog
properly, a query layer over both stores, and the agents the ground
truth was built for — each phase with its numbers, or it doesn't ship.
