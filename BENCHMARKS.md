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
edge clear + one round trip per relationship), plus transaction
begin/commit overhead per message ≈ six to seven sequential round
trips per message, with **~80% of wall time in the driver's receive
path — over a third of it raw socket wait in `socket.recv_into`**, the
rest the driver buffering and parsing the responses it waited for. The
process was waiting, not working; Memgraph was most likely idle
between statements (inferred from the client-side profile — the server
side was not measured).

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
and stays out of scope for a single-consumer budget row — its safety
under concurrent watermark writes is untested and tracked in #32.
**D4 ingest memory ceiling (<512 MB RSS): validated with ~6× headroom**
— peak 82 MB, flat across run lengths (the consumer holds one batch,
never the stream).

**Contended capacity — solo numbers carry an implicit "with nothing
else running".** The table above measures the hot consumer with the
rest of the platform quiet. With the full stack co-located on the same
laptop — cold consumer committing to Iceberg, Prometheus + Grafana
scraping, publisher producing — sustained hot-ingest capacity drops to
**~3,500 updates/s**, roughly a third of the solo figure (observed
during the INC-001 chaos drill, `docs/incidents/INC-001-hotstore-loss.md`,
which pinned its load at 2,500/s for exactly this reason). Any capacity
plan built on a benchmark row must first ask what else shares the
machine.

## Cold store — append rate, event-time travel, storage (D11–D13, D4)

**Hardware:** Apple M3, 8 GB RAM, macOS 15.2 (laptop; everything
in-process — no containers involved in the cold path).
**Date / PR:** 2026-08-01 / #43.
**Method:** `benchmarks/cold_bench.py` — seeded 10k-world stream
appended in batches; the append clock covers Arrow conversion + the
Iceberg commit only (generation excluded). As-of latency: p50 of 3 at
fixed fractional positions through a 1M-event history, with snapshots
at the 25/50/75% marks and without any. Storage from file sizes on
disk, data (parquet) and total (parquet + Iceberg metadata) reported
separately — the distinction turned out to be the finding.

| Measure | N | Batch | Result |
|---|---|---|---|
| append | 200k | 1,024 | 24.3k events/s |
| append | 200k | 8,192 | **194k events/s** |
| append | 200k | 65,536 | 222k events/s |
| append, sustained | 1M | 1,024 | **5,635 events/s** (see compounding note) |
| append, sustained | 1M | 8,192 | **136.5k events/s** |
| `state_at` p50, snapshots on | 1M | — | **0.17–0.39 s** (25%→95% marks) |
| `state_at` p50, pure replay | 1M | — | 0.21–0.55 s |
| snapshot materialization | 1M | — | 0.54 s each |
| storage, data / total | 1M | 8,192 | **18 MB / 25 MB** (123 files) |
| storage, data / total | 1M | 1,024 | 22.9 MB / **363.5 MB** (977 files) |
| storage, data / total | 200k | 1,024 | 4.1 MB / **20.8 MB** (196 files) |

### Findings (the numbers argue with each other, usefully)

**Commit granularity inverts the hot store's knee — and the penalty
compounds.** The hot ingest measured 1,024 as its batch sweet spot
with 2,048 regressing; the cold path is the opposite, and worse than
the sweep suggests. At 200k events, batch 1,024 costs 8× the
throughput of 8,192. Run the same configuration to 1M and it degrades
to **5,635 events/s — 24× slower** — because each Iceberg commit
rewrites metadata that grows with the table's accumulated snapshots
and manifests: the small-batch tax is not a constant, it compounds
with table history (177 s of append time at 1M vs the ~41 s the 200k
rate would predict). Same stream, two sinks, opposite optima — and a
sweep at one scale does not extrapolate; the 1M row exists because a
review question forced the measurement.

**Commit granularity is also a storage decision.** Same million
events, same method: **363.5 MB total at batch 1,024** (977 files)
versus **25.2 MB at 8,192** (123 files) — while the parquet data is
18–23 MB either way (the spread itself is small-files row-group
overhead). The data was never the problem; a thousand commits'
metadata was. (An earlier ad-hoc ingest showed ~366 MB by `du` on
mixed stream content — directionally right, methodologically loose;
the numbers above are the same-generator, same-method measurement.)

**Replay is already fast; snapshots buy the tail.** Pure event replay
answers `state_at` on a 1M-event history in 0.21–0.55 s — DuckDB over
18 MB of parquet does not need help at this scale. Snapshot
acceleration matters where the replay is longest (0.55 → 0.39 s at the
95% mark) and its value grows with history length; at laptop scale it
is a correct-by-property-test optimization waiting for a bigger world.

**D4 cold budgets: all three validated, no supersession** — append
136.5k vs ≥10k (13×), as-of 0.39 s vs < 2 s (5×), storage 25 MB vs
< 500 MB (20×), each at the documented batch size. First phase where
every provisional budget held; the margin says the budgets were set
timidly after two phases of misses, which is itself calibration data.

## Query layer — composite split, push-down delta, live endpoints (D15–D16, D4)

**Hardware:** Apple M3, 8 GB RAM, macOS 15.2 (laptop; Memgraph in
Docker for the live rows, cold path in-process).
**Date / PR:** 2026-08-01 / phase 5.
**Method:** `benchmarks/query_bench.py` — seeded worlds at four sizes;
composite timed per step (cold reconstruction / BFS traverse /
result filter), p50 of 7. Push-down delta on the `/world` route: the
same `attrs.zone=z1` predicate evaluated in the DuckDB scan vs forced
into the Python residual path, identical results asserted by the test
suite. Live rows: p50 of 200 requests through the full API path
(in-process TestClient over a real Memgraph, 5k-resource world +
20k churn), so route + validation + serialization are included.

| Measure | World | Result |
|---|---|---|
| composite as-of blast radius, p50 | 10k res / 1M events | **0.250 s** with projection push-down (reconstruct 0.24, traverse 0.009); **0.371 s** without |
| composite as-of blast radius, **p95** (n=40) | 10k res / 1M events | **0.393 s** — measured for D18: SLO threshold = 1.5×p95 → 0.6 s |
| composite as-of blast radius, p50 | 10k res / 500k events | 0.185 s |
| composite as-of blast radius, p50 | 5k res / 100k events | 0.074 s |
| `/world` pushed vs residual, p50 | 10k res / 1M events | **0.145 s vs 0.367 s (2.5×)** |
| `/world` pushed vs residual, p50 | 5k res / 100k events | 0.041 s vs 0.074 s (1.8×) |
| `/world` pushed vs residual, p50 | 1k res / 10k events | 0.025 s vs 0.033 s (1.3×) |
| live `/blast-radius` end-to-end, p50 | 5k res + 20k churn | **2.5 ms** |
| live `/resources/{id}` end-to-end, p50 | 5k res + 20k churn | 2.2 ms |
| `plan()` + `explain()`, p50 | — | **0.012 ms** |

### Findings

**The composite is a reconstruction with a rounding error attached.**
At every size, cold `state_at` is 94–97% of the composite; the BFS
traverse over the reconstructed world is ~10 ms even at 30k alive
resources. Optimizing this query means optimizing reconstruction
(snapshot cadence, partition pruning) — the ephemeral-graph half of
D16 never becomes the problem at this scale.

**The push-down delta grows with scale — and the cost is the
boundary, not the predicate.** 1.3× at 10k events, 2.5× at 1M. The
residual path doesn't lose by evaluating predicates slowly; it loses
by materializing every row into Python (Arrow → dicts → JSON parse)
before the filter can look at them, and that serialization tax scales
with world size while the pushed filter's cost scales with matches.
Push-down is less about where the comparison runs than about how much
data crosses the engine boundary.

**Projection push-down arrived by book review, and the benchmark had
already argued for it.** Reading Grove's *How Query Engines Work*
after the phase shipped exposed the gap: the scan contract there is
`scan(projection)` — columns first, predicates second — while this
planner pushed predicates and then JSON-parsed `attrs` for every one
of 30k reconstructed rows to return four. Pruning columns at the
Iceberg scan (`projection` + `where_cols` through `state_at`) cut the
composite from 0.371 s to **0.250 s** at 1M events (−33%): the win
splits between the narrower scan (0.275 → 0.238 s reconstruct) and no
longer materializing attrs through the Arrow→Python boundary in the
result step. The finding above said the boundary tax was the cost;
this is the same finding acted on for columns, not just rows.

**D4 query budgets: all validated, no supersession** — live p50
2.5 ms vs < 100 ms (40×), composite 0.250 s at 1M vs < 2 s (8×),
explain 0.012 ms vs < 50 ms with zero store contact (asserted by
test, not just timed). Second consecutive phase with every budget
holding; the margins stay large, which now reads less like timidity
and more like laptop-scale queries simply being cheap — the budgets
exist for the day they stop being.

## Tool payloads — refs+cap vs fat responses (D19–D20)

Method: `benchmarks/tool_payload_bench.py`. Per world size (1k / 10k /
100k resources, seed 42, churn = world size): seed memgraph, sample 30
live roots, measure the canonical `blast_radius` response (refs, token
cap) against the same traversal serialized fat (full attrs via
`with_attrs=True` — the route-shaped payload the tool refuses to be).
Tokens = len(json)/4, the same estimate the cap enforces. Cold-backed
tools measured at 10k via a temp catalog fed the identical stream.
Hub row: a constructed 900-dependent host appended to the 100k world —
seed-42 radii top out at ~30 nodes, so the natural worlds never stress
the cap; the hub is what the cap is FOR. Run 2026-08-02.

| Measure | 1k world | 10k | 100k |
|---|---|---|---|
| `blast_radius` refs, p50 / p95 / p100 tokens | 54 / 292 / 676 | 41 / 186 / 493 | 47 / 162 / 544 |
| `blast_radius` fat, p50 / p95 / p100 tokens | 48 / 836 / 2,030 | 0 / 462 / 1,448 | 25 / 392 / 1,610 |
| `fetch_resource` p50 / p100 | 70 / 115 | 70 / 114 | 70 / 114 |

| Hub (900 dependents, in the 100k world) | tokens |
|---|---|
| refs response (379-ref page, `truncated: true`, `total_count: 900`) | **7,172** — under the 8,000 cap |
| fat response (all 900 nodes, full attrs) | **53,775** — 6.7× the cap, linear in fan-out |

Two findings. **The cap only earns its keep on hubs** — at seed-42's
natural radii (≤ ~30 nodes) refs vs fat is a 3–4× constant factor at
p95+ and both fit any context window; the flat-p100 claim is really a
claim about the hub row, where fat is unbounded (linear in fan-out)
and the refs response is capped by construction, with pagination
picking up the remainder. **Detail is flat everywhere** —
`fetch_resource` p100 ≈ 114 tokens at every world size, which is what
makes the refs+fetch contract work: following a ref costs the same in
a 1k world and a 100k one.

Latency note: the canonical layer adds shaping + validation over the
phase-5 query path; measured overhead is sub-millisecond against the
2.5 ms live endpoint p50 above — not separately tabled.
- Gateway serving capacity (knee, TTFT, admission behavior): docs/capacity.md — method + hardware there; laptop-scale only.

## Replay cache — both layers, per traffic class (D32)

The gateway's two cache layers measured separately on replayed REAL
traffic, per the D32 rule (never synthetic prompts). Method: the same
eval item run twice through the running gateway (`resgraph-evals run
--worker qwen-replay-gateway --no-judge`, response cache ON — the
replay setup, never a measured-run arm), `/metrics` snapshotted between
passes (`scripts/replay-traffic.py`). Provider-layer row: the committed
gateway-pilot receipt (two identical analyst-shaped calls pinned to
haiku through the same hop). Hardware as in docs/capacity.md (Apple M3,
8 GiB, Docker VM ~3.8 GiB). Run 2026-08-15.

| traffic class (layer) | pass 1 (cold) | pass 2 (replay) | backend not spent |
|---|---|---|---|
| workhorse, temp-0 local (gateway layer) | 0/5 hits — 5 writes | **4/4 hits** | 13,634 tokens; 432.6 s of generation served in 0.07 s |
| judgment, sampled paid (gateway layer) | ineligible by design | ineligible by design | — (a replayed sampled draw would be a quiet lie) |
| judgment, sampled paid (provider prefix layer) | 0/1 — `cache_creation` 4,800 | **1/1** — `cache_read` 4,800 | 4,800 prefix tokens re-priced at 0.1× |

The replay hit the full recorded trajectory byte-for-byte — the same
tool call, the same report attempts, the same validation failures —
because nothing the tools returned changed between passes: the cache
converges with the world as observed, it never invents. The cold pass
wrote one entry more than the replay consumed (5 writes, 4 replayed
lookups; the recorded run has 4 turns) — bounded at one request and
not attributable from the run row, noted rather than explained away.
Cost delta in dollars: $0 on the local class by construction; the
provider class re-prices its prefix at 0.1× on the replay leg.
