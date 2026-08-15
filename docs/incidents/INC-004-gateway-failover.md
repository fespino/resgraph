# INC-004: local backend killed mid-run — the walk, the pins, and the price of falling forward (induced)

**Status:** resolved — measured; two findings filed as follow-up issues
**Induced:** yes (`scripts/gateway-drill.py` + operator kill/restore, run 2026-08-15)
**Impact:** no production impact — the subject is the gateway under an induced backend death. Spend: ~$0.088 ($0.006 pilot + $0.080 drill + ~$0.002 probes, estimated) against a ~$0.15 registration.
**Evidence:** `docs/incidents/inc004/` (pilot result, drill JSONL gzipped, kill/restore marks, readout, health-transition log, metrics scrape, supplementary mid-stream kill); pre-mortem `docs/drills/premortem-inc004-failover.md`.
**Code under test:** `f45cfe6`.

## What was supposed to happen

The pre-mortem's five claims, each re-derived from the code before spend: (a) in-flight streams die with a structured `stream_error`, never a splice; (b) new non-streamed routed traffic falls forward to the paid backend with the hop recorded on every response; (c) pins never substitute — dead-backend pins fail loudly, the pinned judge is untouched; (d) routed traffic recovers the moment the backend serves again, while fallback candidacy waits for gradual readmission; (e) the fall-forward window has a measurable $/hour.

The chain-verification pass had already corrected the phase plan on three points before any spend: streamed traffic cannot fall forward (the anthropic stream factory returns 501 — no streaming adapter exists), so the paid fall-forward is non-streamed only and the zero-token restart rule has no live target in this topology; and the `GatewayDegradedChains` alert cannot fire from a served two-backend request.

## What happened

Five lanes of analyst-shaped traffic (two streamed workhorse, one non-streamed workhorse, a pin to the local model, a pin to haiku — every request `cache_responses: false` with a nonce), 8 minutes, kill at t+120s, restore at t+300s.

| T (UTC, 2026-08-15) | Event |
|---|---|
| 09:53 | Pilot with ollama dead: ONE routed request falls forward — `backend: anthropic`, `source: task_class_default`, `chain: ["ollama:qwen-local-1.5b"]` — PASS, full drill authorized |
| 09:54 | Drill starts; baseline clean: all routed lanes on ollama, streams ending honestly, TTFT p50 ~21s at this concurrency |
| 09:56:24 | `docker compose kill ollama` (SIGKILL); in-flight streams die at +1.9s |
| 09:56–09:59 | Outage: the non-streamed lane serves 47/47 from anthropic with the hop recorded; pin-local fails 502 × 18 with zero substitution; the judge lane runs 9/9 on anthropic untouched; the streamed lanes return 11,231 structured `stream_error`s in 182s — a hot loop (below) |
| 09:59:26 | `docker compose up -d ollama`; routed traffic returns to ollama before health readmits it (the walk attempts the routed model regardless of health state); readmission is gradual and flappy during model reload, converging to healthy |
| 10:04 | Supplementary kill, targeted mid-generation: 5 tokens delivered, then `stream_error` with `tokens_emitted: 5` and the transport reason verbatim — no restart, no splice |

Headline numbers, before interpretation:

- **Fall-forward: 47/47** outage requests on the non-streamed routed lane served by anthropic, every response carrying `source: task_class_default` and the chain naming the failed hop. Latency p50 dropped from 22.7s (local) to **1.7s** (haiku) — falling forward was faster, at a price.
- **The price: $0.0544 measured in 182s → $1.08/hour** extrapolated (arithmetic, not a projection), at this drill's traffic level (~15 non-streamed workhorse requests/minute). $0.00113/request — the shared 4.4k-token prefix was written once by the pilot and read by every subsequent paid call, so this is the warm-cache price; a cold-cache hour would cost ~6× more.
- **Pins: zero substituted calls** across 11,452 recorded responses. Pin-to-dead-backend: 18 loud 502s (`pin_failed_502` in the request counter). The pinned judge: 22/22 on anthropic, unaffected throughout — the audit query is a one-liner over the drill JSONL.
- **Splices: impossible and none observed** — zero streams ended `ok` with a non-empty chain; the one mid-generation death surfaced `tokens_emitted: 5` and ended.
- **Metrics saw the outage honestly**: all 11,324 stream errors counted (`outcome="stream_error"`), buckets split exactly as induced — 11,323 `zero`, 1 `nonzero`.

## Findings (the reason to run the drill against code, not the design doc)

1. **The streamed outage path is honest but has no backpressure — filed.** The stream open is lazy, so a dead backend surfaces inside the relay as a zero-token death; the reopen walk finds anthropic unstreamable (501) and exhausts; the client gets HTTP 200 + a structured `stream_error` in ~15ms. Honest at every step — and nothing tells the client to slow down. Our own drill client, which dutifully honors `Retry-After` on 429, hammered 11,231 requests through this path in 3 minutes. Queue admission never engaged because each request holds its slot for milliseconds.
2. **Two-hop chains exist, but only the log sees them — filed.** `[gateway:degraded] fallback chain length 2: ['ollama:qwen-local-1.5b', 'anthropic:haiku']` fired ~11k times. The fallback-chain histogram, though, is recorded only on *successful* ends — a request that degrades through the whole walk and dies never reports its chain length — so the `GatewayDegradedChains` alert stayed quiet through a textbook degradation. The pre-mortem predicted the alert's silence for the wrong reason ("chains can't exceed 1"): chains do exceed 1 on the stream reopen path; they're just invisible to the metric.
3. **Recovery has two clocks, as the code said.** Routed traffic returned to ollama on the first attempt after the container served again — the walk tries the routed model regardless of health state — while probe-driven readmission flapped through `slow`/`fail` during model reload before converging. Recovery TTFT carried the registered first-request model-load spike (streamed p95 48.5s vs 26.8s baseline) and was still settling when the window closed; steady state returned after the drill.
4. **The paid backend's probes flapped under load.** Anthropic probes intermittently exceeded the slow threshold (and once failed) during the drill, cycling healthy→degraded→healthy with no serving impact. Probe verdicts are load-sensitive; worth remembering when probe cadence changes (#209).

## What is established, and how strongly

- **Established directly:** init-failure fall-forward with the hop recorded per response; pin no-substitution under backend death; mid-stream death honesty (structured error, no resume); the two-clock recovery; the warm-cache fall-forward price at this traffic level. Each is one grep over committed evidence.
- **Established structurally:** splicing is impossible by construction (no resume path exists in the relay) — the drill adds the observation that none occurred under 11k+ failure events.
- **Not established:** fall-forward behavior for streamed traffic (cannot exist until an anthropic streaming adapter lands); the $/hour at any other traffic level (scales with routed non-streamed volume; laptop-scale, one lane); alert-level visibility of degradation (finding 2 — the gap is the result).
