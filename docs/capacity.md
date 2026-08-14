# Gateway capacity — the load test and the knee

**Hardware:** Apple M3, 8 GiB RAM, macOS (Darwin 24.2); ollama in Docker
(VM capped at ~3.8 GiB, 8 vCPU). **Model:** qwen2.5:1.5b (Q4_K_M) via
ollama — the local backend, pinned (`pin=qwen-local-1.5b`, so the paid
backend is unreachable by construction). **Numbers are laptop-scale and
claim nothing beyond this host.**

## Method

`scripts/gateway-load.py` against a running `resgraph-gateway`:
analyst-shaped streamed requests (the real ~3.6k-token system prefix +
the registry's 5 tool blocks ≈ 4.4k-token prefix, `max_tokens` 64),
`cache_responses: false` and a per-request nonce so no cache layer sits
in the measurement path (the trap registered before this run: replayed
byte-identical traffic would have measured the response cache, not the
server). One warmup request loads the model off the clock. Steps of
**45 seconds** (not the 10-minute steps a fleet test would use — stated
plainly; n per step is 9–97) at concurrency 1, 2, 4, 8. The client
honors `Retry-After` — a load test that ignores the gateway's own
contract tests a client that shouldn't exist. Raw rows:
`docs/capacity-load-results.json`.

## Results

| c | n | ok | 429 | TTFT p50 | TTFT p95 | agg tokens/s |
|---|---|----|-----|----------|----------|--------------|
| 1 | 10 | 10 | 0 | 0.54 s | 11.7 s | 6.1 |
| 2 | 15 | 15 | 0 | 2.7 s | 33.2 s | **8.7** |
| 4 | 9 | 9 | 0 | 22.7 s | 26.0 s | 5.3 |
| 8 | 97 | 9 | 88 | 18.7 s | 27.8 s | 5.7 |

## The knee, and what it is made of

**Aggregate throughput peaks at concurrency 2 and falls at 4, while
TTFT p50 rises 2.7 s → 22.7 s: the knee sits between 2 and 4.** The
lever is the model server, not the gateway — the gateway added routing,
admission, and accounting; the serialization is ollama's. Beyond the
knee, the bounded queue (in-flight cap 4 for the local backend) turns
overload into fast, honest 429s with a drain-derived `Retry-After`:
at concurrency 8 the client saw 88 rejections, zero errors, and
throughput held ~5.7 tokens/s — degradation is loud and flat, not a
latency collapse.

Two shapes worth keeping:

- **TTFT at c=1 is bimodal — p50 0.54 s vs p95 11.7 s** — ollama reuses
  the KV prefill for a repeated prefix; a cold prefill of the 4.4k-token
  prefix costs ~11–12 s on this host. The mean (~4 s) describes no
  request that ever happened; p50/p95 or nothing.
- **Prefill dominates TTFT.** The analyst's real prefix is the cost of
  the first token; short steps and small n are stated rather than
  averaged away.

## Serving objectives (D18 discipline: measured × 1.5)

From the healthy region (c ≤ 2) of this run, for the **local backend**:

- TTFT p95 objective: **≤ 50 s** (33.2 × 1.5).
- Aggregate throughput floor under load: **≥ 5 tokens/s**.

The anthropic backend's objectives are deliberately not set here — the
pilot produced two samples, and two samples are not a baseline. They
land when a measured run produces a distribution.

## What breaks at 1000×

The knee moves inside the accelerator (continuous batching, paged KV),
queue depth becomes multidimensional, and failover is capacity math —
per the phase charter. Nothing in this file extrapolates to that.
