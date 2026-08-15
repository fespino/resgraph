# Pre-mortem: the eval suite through the gateway — gate item ①'s second half

*Written before the run. One item, k=1, `--worker haiku-via-gateway`, judge
on Anthropic directly — the standard pilot size IS the run (~$0.15).*

**Claim under test:** the eval suite runs end-to-end through the gateway —
the same CLI, grading, and row schema as every certified run — with the
winning routing source recorded on every call in the row's `llm_trail`
(`source: pin`, `backend: anthropic`, `cached: false` per call). The
analyst half of gate item ① has its receipt from the pilot; this collects
the suite half.

**Estimated cost:** ~$0.15 (haiku worker ~$0.02 + judge). Registered before
spend, per the runbook.

## How this could complete, produce numbers, and measure nothing

| Measure-nothing mode | Guard |
|---|---|
| The gateway's response cache serves a trial — pass^k collapses toward pass@1 and the row measures the cache, not the model | `haiku-via-gateway` pins with `cache_responses: false` (the committed instrument bypass); every `llm_trail` entry must show `cached: false` |
| The multi-turn or CLI path crashes mid-run and the spend bought a stack trace | Both paths just ran end-to-end for $0 on the local replay worker — the same client code, CLI wiring, and row assembly, two bugs already found and fixed there |
| The row records intent (the pinned setup) but not the per-call outcome, and the "audit trail" claim rests on inference | `llm_trail` landed this branch and was verified live on the $0 run; the pass condition reads it, not the setup |

## The causal chain

- The CLI resolves the gateway worker: `--worker haiku-via-gateway` →
  `build_client` → `GatewayClient` (`src/resgraph/evals/providers.py:402`),
  pin and bypass from the setup.
- Every harness turn POSTs `/v1/generate`; the pin resolves with no
  fallback allowed (`src/resgraph/gateway/router.py:68`); source, backend,
  and cache state ride back on the Response.
- The harness emits them per call (`run_triage`,
  `src/resgraph/analyst/harness.py:322`) and the runner records them in
  the row (`llm_trail`, `src/resgraph/evals/runner.py:420`).
- Grading, provenance, and the sanitize sweep run unchanged — the row is a
  normal row with the resolved worker setup embedded.

## Pass condition

The run completes with a written row; every `llm_trail` entry shows
`source: pin`, `backend: anthropic`, `cached: false`; the worker
provenance names `haiku-via-gateway` with its pinned wire model; usage and
cost land per the row's usual fields. The verdict of the item is NOT the
subject — haiku's quality is certified elsewhere; this receipt is about
the path. Fail → STOP, diagnose from the row and the gateway log.

## The commands (after review, on go)

```
uv run resgraph-gateway &      # anthropic key in env; probes on
head -1 evals/scenarios/base.jsonl > /tmp/receipt-item.jsonl
uv run resgraph-evals run --scenarios /tmp/receipt-item.jsonl --trials 1 \
  --worker haiku-via-gateway --out-dir evals/runs
```

## Outcome (2026-08-15, one run, ~$0.12)

PASS on all conditions: `evals/runs/20260815T231345Z.jsonl` — 7/7 calls
`source: pin` / `backend: anthropic` / `cached: false` in `llm_trail`,
provenance naming the setup and its pinned wire model, dims graded, 13
tool calls, 17.8s. The provider prefix layer carried the growing
transcript through the hop (`cache_read` 39,298 / `creation` 8,594 on a
7-turn run). The $0 replay collection earned its keep beforehand: the two
crashes it surfaced (the CLI's missing-model KeyError, the unserializable
echoed blocks) would each have been this run's stack trace.
