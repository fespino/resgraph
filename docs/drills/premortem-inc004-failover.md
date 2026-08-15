# Pre-mortem: INC-004 — the gateway failover drill

*Written before the run. See the [drill runbook](README.md). Kill the local
backend mid-run at ~70% of knee concurrency (knee = 2, docs/capacity.md, so
the drill runs at 2 concurrent streams + 1 non-streamed lane), watch the
walk, the pinned honesty, the TTFT curve, and the $/hour of falling forward
to the paid API. Restore, watch readmission.*

**Claim under test:** when the local backend dies mid-run, (a) in-flight
streams surface a structured `stream_error` — never a splice; (b) new
non-streamed routed traffic falls forward to the paid backend with the hop
recorded on every response; (c) pinned requests never substitute — pins to
the dead backend fail loudly, the pinned judge is untouched; (d) recovery is
immediate for routed traffic and gradual for fallback candidacy; and (e) the
fall-forward window has a measurable $/hour.

**Estimated cost:** ~$0.15 registered (pilot ~$0.006 + fall-forward lane
~$0.07 + judge lane ~$0.03 + probes, all on haiku with a shared warm
prefix). Brake: the drill script hard-caps paid-served requests at 120
(~$1 worst case if every request were a cold cache write).

## What the code says the drill will show (verified against main, not the design doc)

Three facts the phase plan did not know, found by reading the code before
spending. They are registered here so the drill measures what exists:

1. **Streamed traffic cannot fall forward.** The stream factory refuses
   anthropic (`_default_stream_factory`,
   `src/resgraph/gateway/server.py:314` — 501, "send stream=false"), and
   both the init walk (`server.py:265` re-raises `HTTPException`) and the
   mid-stream `reopen` (`server.py:474`) end there. During the outage,
   streamed requests get a 501 after a one-hop walk; the paid fall-forward
   is observable only on the non-streamed lane. Consequence: the
   zero-token restart rule has **no live target** in this topology — a
   zero-token death surfaces `stream_error{tokens_bucket="zero"}` after an
   exhausted reopen walk. The silent-restart rule stays proven offline by
   the relay tests.
2. **A walk that ends in that 501 is unmetered.** `rejected_429`,
   `pin_failed_502`, and `exhausted_503` all increment `GATEWAY_REQUESTS`;
   an `HTTPException` raised by the attempt re-raises with no counter
   (`server.py:265`). The streamed lane's outage failures will be visible
   to the client and in the fallback log but invisible to availability.
   Registered as an expected observability finding → issue after the drill.
3. **`GatewayDegradedChains` cannot fire here.** The alert triggers on
   chains **longer than one hop** (`observability/rules/gateway_slo.yml:37`);
   with two routed backends a served request's chain is at most 1. The
   fired signals for this drill are the `[gateway:fallback]` log line
   (`server.py:230`) and chain-histogram mass at 1 — not the alert.

## How this could complete, produce numbers, and measure nothing

| Measure-nothing mode | Guard |
|---|---|
| The fall-forward premise is wrong for non-streamed traffic too, and the outage lane just errors — a run full of failures priced at $0 | The pilot: with ollama dead, ONE non-streamed `task_class: workhorse` request must return `backend: anthropic`, `source: task_class_default`, `fallback_chain: ["ollama:qwen-local-1.5b"]` before the full drill spends anything else |
| The gateway response cache serves the fall-forward lane and haiku is never hit — $/hour measured as ~0 | `cache_responses: false` + a per-request nonce in the user message on every lane (the registered replay trap: k trials from a cache would collapse the measurement) |
| `docker compose stop` shuts ollama down gracefully and in-flight streams finish — no mid-stream death to observe | `docker compose kill` (SIGKILL), sent while the streamed lane is mid-generation (streams run ~10s near-continuously at c=2; assert post-hoc that ≥1 `stream_error` with `tokens_emitted > 0` was received, else repeat the kill — the local-only half of the drill is free to repeat) |
| The client's timeout expires during the slower paid calls and client-side errors masquerade as gateway outcomes | httpx timeout 120s on every lane, far above haiku's non-streamed latency |
| Recovery numbers measure ollama's model re-load, not steady state | Registered: the first post-restore request carries the model load; the recovery TTFT is reported with that first-request spike labeled, and the phase runs long enough (~3 min) to show steady state after it |
| The judge lane and the fall-forward lane share no prefix and every paid call pays a cold cache write — cost numbers inflated ~10× | Both lanes build the identical system + tool blocks (same constants); the first write warms the prefix for all subsequent paid calls; per-request usage (cache_read/creation) is recorded raw so the incident note prices what actually happened |

## The causal chain

- The kill reaches in-flight streams: SIGKILL closes the TCP stream under
  `parse_chat_sse`, the iterator raises, the relay catches it
  (`src/resgraph/gateway/relay.py:93`), and `StreamAccount.died` rules
  restartability — `restartable = content_tokens == 0`
  (`src/resgraph/gateway/accounting.py:82`). Tokens emitted → structured
  `stream_error` with `tokens_emitted` (`relay.py:104`); zero tokens →
  `reopen` walk (`server.py:474`) → anthropic 501 → exhausted → the same
  structured error, `tokens_bucket="zero"`.
- New non-streamed workhorse traffic walks: `_serve_with_walk`
  (`server.py:241`) attempts `qwen-local-1.5b`, the connection fails,
  `_record_hop` logs `[gateway:fallback]` (`server.py:227`), `_next_alias`
  (`server.py:210`) offers anthropic's serving alias (`haiku` — routed via
  the judgment class, `src/resgraph/gateway/router.py:30`), and the
  response records `backend`, `source`, and the chain (`server.py:526`).
- The fall-forward is priced per response: `GATEWAY_COST` with
  backend/source/task_class labels (`server.py:163`), and the drill script
  records raw usage per response for `estimate_cost`
  (`src/resgraph/evals/pricing.py`).
- Pins never substitute: `fallback_allowed=False` on pin
  (`router.py:68`), so a pin to the dead backend raises `pin_failed_502`
  without walking (`server.py:268`); the judge's pin resolves to haiku and
  never touches ollama.
- Health sees the outage: the probe is a real generation
  (`server.py:323`), a failed probe sets `down` immediately, and
  readmission takes 3 consecutive passes
  (`src/resgraph/gateway/dispatch.py:27`), visible as `[gateway:health]`
  transitions (`server.py:349`).
- Recovery of routed traffic does NOT wait for readmission: the walk
  always attempts `decision.model` first (`server.py:248`), so local
  serving resumes the moment ollama generates again; readmission gates
  only fallback candidacy.

## Pass conditions

1. **Mid-stream honesty:** ≥1 `stream_error` with `tokens_emitted > 0`
   received by the streamed lane at the kill; no stream in the record both
   emitted tokens and continued on another backend (splice impossible).
2. **Fall-forward:** every outage-window response on the non-streamed
   routed lane shows `backend: anthropic`, `source: task_class_default`,
   chain naming ollama; after restore, `backend: ollama` returns.
3. **Pin honesty (the audit):** over every recorded response,
   `source: pin` ⇒ served by the pinned setup's backend or failed 502 —
   zero substituted calls; ≥1 `pin_failed_502` observed on the pin-local
   lane during the outage; the judge lane unaffected throughout.
4. **The curve and the price:** per-phase TTFT/latency (baseline → outage
   → recovery) and the outage window's measured paid spend, reported as
   $/request and extrapolated to $/hour labeled as arithmetic.
5. **Health transitions** in the gateway log: `up→down` after the kill,
   `down→…→healthy` after 3 passing probes post-restore.

Fail on any → STOP, diagnose from the recorded JSONL and gateway log
before any second paid attempt.

## The commands (after review, on go)

```
uv run resgraph-gateway > /tmp/inc004-gateway.log 2>&1 &   # probes on
# pilot (ollama dead, one request, ~$0.006):
docker compose kill ollama
uv run python scripts/gateway-drill.py --pilot pilot.json
docker compose up -d ollama                                 # restore, wait healthy
# full drill (~8 min: 120s baseline, kill, 180s outage, restore, 180s recovery):
uv run python scripts/gateway-drill.py drill.jsonl
docker compose kill ollama       # at the baseline→outage mark
docker compose up -d ollama      # at the outage→recovery mark
```
