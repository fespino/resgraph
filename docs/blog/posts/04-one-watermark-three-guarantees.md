---
date: 2026-07-31
categories:
  - Data platform
tags:
  - ingest
  - streaming
  - idempotency
  - benchmarks
  - profiling
---

# One watermark, three guarantees

Every streaming pipeline faces the same three demons: messages arrive
more than once, messages arrive out of order, and consumers crash
mid-batch. The industry reflex is to fight them in the transport —
deduplication tables, ordering buffers, exactly-once delivery machinery
that is expensive, fragile, and famously hard to get right. resgraph's
ingest takes the other route: **make the store immune instead of making
the delivery careful.** One integer per resource — a watermark — buys
all three guarantees, and this post shows the property tests that prove
it, the crash test that demonstrates it, and the benchmark that went
from 760 to 12,500 updates per second because a profile disagreed with
the code I'd written.

<!-- more -->

This is the fifth post about **resgraph**, a mini referential data
platform built in public. The generator emits a stream of
infrastructure updates; the graph store answers traversal questions
about the world's current state. Until now, nothing connected them
continuously — the loader bulk-loads a snapshot and refuses anything
else. This phase closes the loop: a consumer reads the update stream
and applies each message into the graph store, correctly, no matter how
delivery misbehaves. It's the phase where the platform's central
reliability claim stops being design prose and becomes a test suite.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-3-ingest`](https://github.com/fespino/resgraph/tree/phase-3-ingest).

## The rule, and what it buys

Each resource node in the store carries `applied_seq` — the sequence
number of the last message applied to it. The whole ingest reduces to
one rule, enforced in the same transaction as the write:

> Apply the message **iff** its sequence is greater than the node's
> `applied_seq`.

Three guarantees fall out:

1. **Replays are no-ops.** A redelivered message has a sequence the
   node has already seen; the watermark skips it. At-least-once
   delivery becomes safe by default.
2. **Out-of-order within a resource is safe.** A stale message arriving
   late has a lower sequence than the node's watermark; skipped.
3. **Deletes are survivable.** A delete writes a *tombstone* — the node
   stays, flagged `deleted`, payload cleared — so a later, higher-
   sequence upsert can revive it. Removal would make "delete then
   late-arriving update" unrecoverable.

The compact way to say all three: the final state of a resource is a
pure function of its **single highest-sequence message**. Arrival
order stops mattering. That property has a name in the test suite —
convergence — and it's the anchor for everything else in this post.

## Tests first, and what the oracle forced

This phase inverted the usual order: the property tests were written
and running (red) before the apply path existed. The centerpiece
generates a random message history for a resource, applies it in a
*random permutation*, replays the whole history again, and asserts the
store landed on the state implied by the highest-sequence message —
for dozens of generated cases, against a live database.

Writing that oracle did something I didn't expect: it **dictated the
write semantics**. Two rules the spec had never stated turned out to be
load-bearing:

- **An upsert replaces the attribute bag; it never merges.** Merge
  looks harmless until you reorder: a stale key from an
  earlier-applied update survives under one arrival order and not
  another. Convergence dies.
- **A tombstone carries no payload.** If a delete left the previous
  attributes in place, the surviving attributes would depend on *which*
  upsert happened to land before the delete. Order-dependent again.

Neither rule came from foresight. Both came from asking "what would
make the oracle fail?" — the test designed the code. They're recorded
as a spec decision now (D10), with the rejected alternative and the
reversal condition, because rules that convergence *requires* deserve
more than a comment.

A post-implementation review caught a third gap the tests had missed:
message attributes share the node's property namespace with the
store's own fields (`deleted`, `applied_seq`, …), so an attribute
literally named `deleted` would be silently overwritten on write and
stripped on read. Silent is the problem — that's now a parse-time
rejection, and the property tests check the `phantom` flag they'd
previously ignored. A reviewer with fresh eyes is a different kind of
property test.

## The consumer: recovery is not a special case

The consumer reads the stream through a consumer group and follows one
ordering discipline: **acknowledge strictly after the apply transaction
commits.** Crash between apply and ack, and the batch is redelivered on
restart — where the watermark skips everything already applied. On
startup, the consumer drains its own unacknowledged entries before
asking for new ones, so resuming from a crash is the same code path as
running normally. No recovery mode, no repair script.

The integration test is the claim made executable: deliver a hundred
messages to the consumer, apply thirty of them, "crash" before any
acknowledgment, restart. Everything is redelivered, exactly the thirty
are skipped, nothing is left pending, and the store matches the
oracle. **At-least-once delivery, exactly-once state** — and the
consumer never has to be careful, because the apply path already is.

## The benchmark: a suspicion, filed in advance

Here's the part I'm most pleased with, process-wise. When the apply
path was first reviewed, its shape — one watermark read, one property
write, one edge clear, plus one round trip *per relationship*, per
message — looked like an obvious throughput problem. The generator
phase taught me what to do with that feeling: **file the suspicion
publicly, then let the profile judge it.** So the concern went into a
PR comment before the benchmark existed, with the candidate fixes and
an explicit "measure first" note attached.

First measurement: **760 updates per second**, 26× under the budget.
The profile confirmed the suspicion with numbers the guess could never
have produced: 23,445 driver statements for 5,000 messages — about 6.7
sequential round trips per message once BEGIN/COMMIT pairs are counted
— and **80% of wall time inside `socket.recv_into`**. The database was
idle most of the time. The *conversation* was the bottleneck, not the
work.

The fix stayed inside the proven semantics: batch messages into one
transaction, group every write into per-label bulk statements, and
resolve intra-batch siblings in Python by keeping each resource's
highest-sequence message — the same verdict the watermark would return,
one round trip earlier. Convergence is exactly what makes that
shortcut legal, and a new property test pins it: batched apply must
equal one-by-one apply for any arrival order *and any batch
boundaries*. About 27 statements per batch instead of ~1,700.

| Path | Messages | updates/s | Peak RSS |
|---|---|---|---|
| per-message apply | 5k | **760** | 79 MB |
| batched, batch 256 | 100k | 9,600 | 76 MB |
| batched, batch 1024 (median of 3) | 100k | **12,500** | 78–80 MB |
| batched, batch 2048 | 100k | 11,000 | 82 MB |
| batched, 1024, longer run | 200k | 10,500 | 80 MB |

Two details in that table earn their place. Batch 1024 is the sweet
spot, and **2048 regresses despite doing fewer writes** — bigger
per-statement payloads cost more than the saved writes return.
Batching has a knee, not a monotonic payoff; the sweep is what finds
it. And the 1024 row is a median of three runs that spanned 11.7k to
14.4k — reporting the median instead of the best run is a small
discipline that keeps the whole table trustworthy.

## Closing the budget, in both directions

The performance budget for the ingest had two rows, written before any
of this existed. They closed in opposite ways:

- **Memory ceiling (< 512 MB): validated with ~6× headroom.** Peak RSS
  82 MB, flat across run lengths — the consumer holds one batch, never
  the stream.
- **Throughput (≥ 20k updates/s): missed at ~12.5k, and amended by
  supersession.** The algorithmic bottleneck is found and fixed; what
  remains is the store executing writes, on a laptop running both
  stores, with message validation deliberately kept on — the same call
  the generator phase made and for the same reason. The budget now
  reads ≥10k sustained single-consumer on laptop hardware, with the
  reasons recorded and the 20k figure retired, not edited away.
  Consumer-group parallelism is the recorded scale-out lever if a
  future phase needs more.

## A side-scar: an open port is not a ready server

One CI failure along the way is worth its own paragraph. Every
store-touching test suddenly died at the connection handshake — in a
setup that had been green for weeks. The readiness check was `nc -z`,
a TCP connect against the published Docker port. But that port answers
as soon as the *proxy* exists, before the server inside the container
is serving anything. One slow container start and the check waved
through a half-started database. The fix: readiness probes must speak
the protocol — the check is now a driver-level handshake that retries
until the server responds, and fails the job loudly if it never does.
**An open port is a fact about the proxy, not the service.**

## What I'd take to the next project

- **Write the oracle before the code.** The convergence test didn't
  just verify the apply path — it *derived* the write semantics
  (replace-don't-merge, payload-free tombstones). When a correctness
  property dictates design decisions, you've found the right property.
- **File performance suspicions publicly, before measuring.** The PR
  comment turned a hunch into a falsifiable prediction with a
  timestamp. The profile then confirmed it — but if it hadn't, the
  correction would be on the record too. Either outcome beats a silent
  guess.
- **Make the store immune, not the pipeline careful.** Every guarantee
  in this phase lives in one transactional rule at the write path.
  The consumer, the retries, the crashes — none of them need to be
  smart, and the tests for them are short because of it.
- **Sweep, don't extrapolate.** "Bigger batches are better" was true
  until 1024 and false at 2048. The knee only shows up if you measure
  past where you expect to stop.

The platform now runs end to end: generator → stream → consumer → graph
store, with traversal queries on top and every reliability claim under
test. Next in this thread: the cold half of the story — snapshotting
the hot store into an Iceberg table so the same questions can be asked
about any point in the past. That phase isn't built yet, so that post
comes when the numbers do.
