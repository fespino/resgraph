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
