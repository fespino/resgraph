# Benchmarks

Every number ships with hardware, method, and date. Laptop numbers are
labeled as such; no scale inflation.

## Emit rate — the generator (D4)

**Hardware:** Apple M3, 8 GB RAM, macOS 15.2 (laptop; Redis in Docker
running alongside for the sink rows).
**Date / commit:** 2026-07-29 / a062dc0.
**Method:** `resgraph-gen run --seed 42 --resources 10000 --count N`;
3 runs, median. Kernel rows measure the churn engine in-process
(no CLI/sink overhead); end-to-end rows go through the CLI.

| Path | N | msg/s (median of 3) |
|---|---|---|
| naive kernel (first implementation) | 2M | **2,200** |
| kernel after index fixes | 500k | **88,000** |
| CLI end-to-end, stdout → /dev/null | 2M | **35,600** |
| CLI → redis, pipelined XADD, batch 500 | 1M | **33,900** |
| CLI → redis, XADD batch 1 | 20k | **7,100** (single run; incl. ~1s startup) |

Redis stream after the 1M run: `XLEN` = 1,000,007, 314 MB — bounded by
`maxlen~` as designed (the stream is transport, not storage).

### Bottleneck notes (the finding is the point)

The naive implementation missed the original ≥100k budget **45×**.
Profiling (cProfile, 100k messages) found the cost was not where
intuition said:

1. **93% of runtime was dangling-edge repair** — every delete scanned
   every alive resource's edge list (88M comparisons across 3k
   deletes). Fixed with a reverse-dependency index (target →
   dependents): repair is now O(dependents). 2,200 → ~80k msg/s.
2. **pick_target rebuilt the hot-alive list per message** (O(hot-set)
   scan × every emit). Fixed with an incrementally maintained index.
3. **JSON serialization was never the problem**: pydantic's Rust
   serializer costs ~2µs/msg (0.19s of 16.2s in the naive profile).
   The guess "it's probably serialization" would have optimized the
   one part that was already fast.

Post-fix the profile is flat — validation, serialization, RNG, and
index maintenance each comparable; no algorithmic win remains. The
next step up would disable message validation (pydantic
`model_construct`), rejected: the generator provably emitting valid
D2 messages is a feature, not overhead.

**Why end-to-end (35.6k) trails the kernel (88k):** (a) the world
grows during a long run — create 5% vs delete 3% ≈ net +2% of
messages, so 2M messages take the world from 10k to ~50k resources
and per-message index costs drift up; visible as degradation across
runs (39.1k → 35.6k → 31.9k), amplified by (b) thermal/memory
pressure on an 8 GB machine also running the Redis container.

**Batch-1 redis row is the round-trip lesson:** per-message XADD
round-trips cost ~5× the pipelined rate. Batching is not optional.

**D4 consequence:** the ≥100k budget is amended by supersession in
SPEC.md — measured reality on this hardware is ~88k kernel / ~36k
sustained end-to-end. A budget without a measurement is a wish; this
one now has a measurement.

*Second machine: pending — rows to be added when the Linux box runs
the same method.*

## Traversal — graph store vs recursive CTE (D1, D4, D8)

**Hardware:** Apple M3, 8 GB RAM, macOS 15.2 (laptop; Memgraph +
Postgres in Docker). **Date / commit:** 2026-07-31 / phase-2 branch.
**Method:** identical seeded graphs in both stores (same snapshot →
Memgraph nodes/edges and a Postgres `edges` table with a `dst` index);
blast-radius depth 3 and 5; 20 targets (10 hub, 10 leaf) × 5 runs;
p50/p95 across all. Cypher `*BFS` DISTINCT vs recursive CTE with
`UNION` (dedup during recursion — the semantics-fair equivalent;
`UNION ALL` would inflate Postgres's work and the conclusion).

| World | Store | Depth | p50 ms | p95 ms |
|---|---|---|---|---|
| 10k | memgraph | 3 | 0.3 | 1.6 |
| 10k | postgres-cte | 3 | 0.2 | 1.8 |
| 10k | memgraph | 5 | 0.2 | 0.4 |
| 10k | postgres-cte | 5 | 0.2 | 0.4 |
| 100k | memgraph | 3 | 0.2 | 0.4 |
| 100k | postgres-cte | 3 | 0.2 | 0.7 |
| 100k | memgraph | 5 | 0.2 | 0.3 |
| 100k | postgres-cte | 5 | 0.2 | 0.3 |

### The finding: a tie — and the bug the first run hid

The expected shape (graph pulls away at depth ≥3 on hub targets) **did
not appear**. At this scale the two stores are statistically
indistinguishable, both sub-millisecond. Why: the working set fits in
memory for both, and blast radii here are small (≤36 nodes at seed 42),
so neither the graph's pointer-chasing nor the CTE's join-per-level is
stressed. The graph store's structural advantage needs bigger worlds,
deeper traversals, or higher fanout to manifest — none of which a
100k-resource laptop fixture exercises. **Where was the CTE
competitive? Everywhere, at this scale.** That is the honest boundary.

The first run reported Memgraph at **17.5ms** — a 40× "loss" that was
entirely a bug in our own query. The tell was flatness: 17.5ms at
depth-3 vs 17.8ms at depth-5 meant the traversal cost nothing and
something *constant* dominated. A `RETURN 1` floor measured 0.27ms
(Bolt is cheap), but a single anchor lookup `MATCH (n {id})` cost
9.4ms — a full node scan. The blast-radius query anchored **without a
label**, so per D8 the per-label `:host(id)` index couldn't be used.
Adding the derived label (`MATCH (x:host {id})`) took the anchor from
9.4ms to 0.31ms and the whole query from 17.5ms to 0.2ms.

**This is D8 earning its rent, the hard way:** the "label per type"
decision only pays if queries *specify* the label — a label-less anchor
silently reintroduces the exact full-scan cost D8's rejected
`:Resource`-with-a-`type`-property alternative would have imposed. The
label is now derived from the id prefix and validated (also the
injection guard); a regression test pins the labeled anchor.

**D4 traversal budget (p95 < 50ms, depth ≤3, 100k world): validated
with ~125× headroom** — measured 0.4ms (Memgraph) / 0.7ms (CTE). Filled
in the D4 table; no supersession needed, the target holds comfortably.

**D1 note:** the benchmark neither disqualifies Memgraph (it meets
budget) nor vindicates "graph beats SQL" at this scale (it's a tie), so
D1 stands on its *other* rationales — in-memory footprint, instant
startup, Cypher/Bolt transferability — not on traversal supremacy. That
is a more honest basis for the decision than the benchmark we expected
to run.

## Ingest — stream → consumer → hot store (D3, D4, D10)

**Hardware:** Apple M3, 8 GB RAM, macOS 15.2 (laptop; Redis AND Memgraph
in Docker alongside — both stores share the machine with the consumer).
**Date / PR:** 2026-07-31 / #29.
**Method:** `benchmarks/ingest_bench.py` — publish a seeded 10k-world
snapshot + churn to a Redis stream, then time the real `resgraph ingest`
CLI in a child process (`--max-messages N`); updates/s = N / wall time,
peak RSS from `getrusage(RUSAGE_CHILDREN)` so the publisher's footprint
never pollutes the consumer's number. Single consumer, message
validation ON, one transaction per batch.

| Path | N | Batch | updates/s | Peak RSS |
|---|---|---|---|---|
| per-message apply (first implementation) | 5k | — | **760** | 79 MB |
| per-label UNWIND batching | 100k | 256 | **9,600** | 76 MB |
| per-label UNWIND batching | 100k | 512 | **12,400** | 76 MB |
| per-label UNWIND batching (median of 3) | 100k | 1024 | **12,500** | 78–80 MB |
| per-label UNWIND batching | 100k | 2048 | **11,000** | 82 MB |
| sustained, longer run | 200k | 1024 | **10,500** | 80 MB |

### Bottleneck notes (the finding is the point)

The first number was 760 updates/s — 26× under the D4 budget. The
suspicion (flagged on PR #29 before measuring) was per-message
chattiness; the profile confirmed it with precision: **23,445
`tx.run` calls for 5,000 messages** (watermark read + property write +
edge clear + one round trip per relationship), plus a BEGIN/COMMIT pair
per message ≈ 6.7 sequential round trips per message, with 80% of wall
time in `socket.recv_into` — the process was waiting, not working, and
Memgraph was idle between statements.

The fix: batch messages per transaction and group every write into
per-label `UNWIND` statements (the snapshot loader's shape). Intra-batch
siblings dedupe to the highest sequence in Python first — the
convergence property makes that verdict identical to the store
watermark's, and a property test pins batched ≡ one-by-one for any
arrival order and any batch boundaries. ~27 statements per 256-message
batch instead of ~1,700. Result: 760 → 12,500 updates/s (16×).

The second profile shows where the remaining time goes: statement
execution inside Memgraph and Bolt parameter packing (~15%) — the
conversation is no longer the bottleneck; the writes are. Batch 1024 is
the sweet spot; 2048 *regresses* despite doing fewer writes (bigger
UNWIND payloads per statement cost more than they save). The 200k run
degrades ~16% vs 100k as the store grows — same mechanism as the
generator's long-run drift, now on the write side.

**D4 ingest throughput (≥20k updates/s, single consumer): MISSED at
~12.5k** — amended by supersession in SPEC.md. The algorithmic
bottleneck (round trips) is found and fixed; what remains is store-side
write execution on a laptop running both stores, with validation
deliberately ON (same call as the generator: provably-valid input is a
feature). Consumer-group parallelism is the designed scale-out lever
and stays out of scope for a single-consumer budget row.
**D4 ingest memory ceiling (<512 MB RSS): validated with ~6× headroom**
— peak 82 MB, flat across run lengths (the consumer holds one batch,
never the stream).
