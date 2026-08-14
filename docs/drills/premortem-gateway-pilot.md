# Pre-mortem: the gateway pilot — the prefix-cache receipt + source in the trail

*Written before the run. See the [drill runbook](README.md). A paid run is a
deploy even at ~$0.03; this one is already the smallest falsifying case, so
the pilot IS the run.*

**Claim under test:** the running gateway preserves the provider prefix
cache through the hop (byte-identical request twice → `cache_read > 0` on
the second response) and the winning routing source rides the seam client
back into the per-call audit event. The offline halves are already proven
(the byte-identical forwarding tests; the audit-event test); this collects
the one thing fakes cannot: the provider's actual cache behavior through a
real serving hop.

**Estimated cost:** ~$0.03 (two haiku calls, ~3.6k-token prefix). No brakes
needed beyond the size of the run itself.

## How this could complete, produce numbers, and measure nothing

| Measure-nothing mode | Guard |
|---|---|
| The gateway's own response cache serves call 2, which never reaches the provider — the pilot would prove the WRONG layer while looking green | `cache_responses: false` in the setup (`haiku-via-gateway`) — the same instrument bypass measured runs use; assert call 2's response has `cached: false` AND `cache_read_tokens > 0` |
| The prefix is below the model's minimum cacheable length — provider returns `cache_creation = 0` on call 1 and the "miss" on call 2 is a non-result | Use the analyst's real system prefix (~3.6k tokens, over haiku's minimum); assert call 1 shows `cache_creation_tokens > 0` — the write is the precondition for the read |
| The two requests differ by a byte (timestamp, ordering) and the miss is our bug, not evidence | Build the body once, send it twice verbatim; on a miss, diff the two recorded request bodies before concluding anything |
| The provider cache TTL lapses between calls | Send the calls back-to-back (seconds apart; TTL is minutes) |

## The causal chain

- The seam client sends the setup's routing and bypass:
  `GatewayClient.create` (`src/resgraph/evals/providers.py`) — `pin`,
  `cache_responses: false` in the body.
- The gateway forwards byte-identically and returns cache usage:
  `_request_kwargs` / `_call` (`src/resgraph/gateway/server.py`) —
  `cache_read_tokens` / `cache_creation_tokens` on `UsageOut`.
- The provider reads its prefix cache on a byte-identical repeat — the
  external fact this pilot exists to observe.
- The source lands per call: the `llm_call` event carries
  `source`/`backend`/`cached` (`run_triage` in
  `src/resgraph/analyst/harness.py`).

## Pass condition

Call 1: `cache_creation_tokens > 0`, `cached: false`, `source: "pin"`,
`backend: "anthropic"`. Call 2: `cache_read_tokens > 0`, `cached: false`.
Both facts recorded in EVALS.md's ledger with the run's request bodies kept
for the diff-on-miss rule. Fail → STOP and diff before any second spend.

## The commands (after review, on go)

```
uv run resgraph-gateway serve &          # probes on; anthropic key in env
# two identical analyst-shaped calls through the seam client, pin=haiku,
# cache_responses=false, the real ~3.6k-token prefix; print both usages
```

## Outcome (2026-08-15, two attempts, ~$0.015)

Attempt 1 **failed exactly through registered mode #2**: the system prefix
alone (3,611 tokens) sits under haiku's 4096-token cacheable minimum —
`cache_creation = 0`, the guard fired, no conclusion drawn from the miss.
The premise "~3.6k is over the minimum" was wrong; the real analyst clears
it because the tool schemas serialize into the prefix. Attempt 2, full
analyst shape (+ the registry's tool blocks, ~4.4k prefix): call 1
`cache_creation 4800`, call 2 `cache_read 4800`, both `cached: false`,
`source: pin` / `backend: anthropic` on both responses and in the per-call
trail. The gateway preserves the provider prefix cache through a real
serving hop — the phase-exit receipt behind the offline byte-identical
tests. Ledger: EVALS.md.
