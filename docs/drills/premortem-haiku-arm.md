# Pre-mortem: the haiku model arm (full base suite)

*Written before the first paid run. See the [drill runbook](README.md). This
is not a fault drill — nothing is induced — but a paid run is a deploy (#100
model arms, D29c), so the same pre-flight applies. The pre-mortem question
shifts from "did the fault fire?" to "does this measure haiku-vs-Opus, or
something else?"*

**Claim under test:** claude-haiku-4-5, run as a worker through the seam, is a
capable enough analyst to be the cheap daily driver — and its found-rate /
cost / latency are comparable to the Opus reference on the *same* items.
**Fault:** none induced. The "premise" that can silently fail is
**comparability**: same item set, same harness fingerprint, real tool use.
**The number:** haiku pass^k and pass@k on the 30-item base set, per-slice
found rates, cost per passed triage, and latency p50/p95 — the haiku row of
the phase-10 arms table.
**Estimated cost:** ~$2.0 (90 rows at the pilot's $0.0222/item, which already
includes one prefix cache-creation, so it is worst-case-ish per item). Brake
at `--max-cost 6.0` (3× headroom for harder items that spend more tool calls),
`--max-item-cost 0.50`. haiku is priced `(1.0, 5.0)` so the cap is armed.

## Causal chain

Every arrow cited. The one you cannot cite is the one that breaks.

| # | Link | Evidence |
|---|---|---|
| 1 | `--worker haiku` resolves to the Anthropic client, model `claude-haiku-4-5`, no thinking | `cli.py:59-63` load_setup + build_client; `providers.py` `_build_anthropic`; haiku setup carries no `extra_args`, so no thinking is sent |
| 2 | the world for each item is rebuilt from its seed and loaded fresh into memgraph | `runner.py:115` `wipe(session)` then load_snapshot + apply_batch, inside `load_stores` called per trial |
| 3 | haiku investigates that world with tools and emits a hedge report | `harness.py:216` run_triage; `harness.py:289` `client.messages.create(**kwargs)` |
| 4 | graders score the report against the planted cause | `runner.py:145` grade_all; `report.py:26` item_passed = found_top3 ∧ evidence for causal items |
| 5 | the row records model, tool_trace, dims, tokens, latency, and the prompt fingerprint | `runner.py:414` cache_fingerprint computed, written at `runner.py:489` |
| 6 | aggregate → pass^k / slices / cost; `arms` compares vs Opus **only if item sets match** | `arms.py:56-57` mismatched item_ids ⇒ `comparable = False`, exit 3 |

**Workload check:** does link 2 hold for *this* workload? Yes — the store is
wiped and reloaded per item unconditionally (`runner.py:115`), so memgraph's
prior contents are irrelevant. This is the exact INC-002 trap (triage reading a
store that holds a different world), and it is closed by construction here. The
1-item haiku pilot already ran this path green.

## How could this run complete, produce numbers, and measure nothing?

| Failure of the experiment | Checked by |
|---|---|
| stale/other world in the store ⇒ triage of the wrong graph | **Closed in code** — `runner.py:115` wipes + reloads per item; pilot confirmed |
| the haiku run's fingerprint ≠ the Opus arm's ⇒ arms confounds model with harness | **Pre-run assertion** — the run must carry a single `cache_fingerprint`, and it must equal the Opus reference arm's. Current instruments = `b041069e` (pilot + recent Opus runs). Grep both run files before calling `arms`. |
| haiku run measures a different item set than Opus ⇒ not a valid comparison | **`arms` declines** — `arms.py:56` compares item_ids, exit 3 on mismatch. Both arms run `evals/scenarios/base.jsonl` (30 items). |
| the cost cap trips mid-run ⇒ a partial run scored as if complete | **Checked** — cap set with 3× headroom ($6 vs ~$2); if it trips, the run is short of 30 items and `arms` will decline it (item-set mismatch). Do not feed a partial run to `arms`. Assert 90 rows before aggregating. |
| numbers computable from runs where haiku never actually used tools | **Checked** — assert every row's `tool_trace` has ≥1 tool call; a no-tool "answer" is a fabrication and the referential/evidence graders fail it (the 1.5b showed exactly this — it did not pass). |
| judge cost/confound inflates or contaminates the arm | **Removed** — run `--no-judge`. pass^k is the deterministic-grader headline (item_passed, `report.py:26`); the narrative dimension needs the judge and is **out of scope** for this cost/capability arm (registered below). |

Nothing in the right column reads "nothing".

## Pilot

- **Smallest falsifying case:** already run — 1 causal item (ambiguous-s42006),
  k=1, `--no-judge`. Result: **passed** (cause in top-3, grounded evidence,
  0 fabrications, 11 tool calls, $0.0222). The pipeline is proven end to end.
- **Pass condition (met):** ≥1 tool call in `tool_trace`, a parsed report,
  no `validation_failures`.
- The full arm is the scale-up of a green pilot, not a first contact.

## What decides the result

- **Halt conditions:** `fabrication_count > 0` on aggregate stops everything
  (unconditional, same as the gate) — a fabricating worker is not a candidate
  regardless of found-rate.
- **Measured, not decisive:** pass^k, pass@k, per-slice found rates, cost per
  passed triage, latency p50/p95. No threshold — haiku is being *characterized*
  against Opus, not gated. (The worker-aware gate, #195, would decline this run
  vs the Opus baseline anyway — it is an `arms` comparison, not a regression.)
- **Not a gate candidate:** a different worker than the baseline's certified
  worker (`gate_skip_reason`, D29c) — so committing it cannot disturb the gate.
- **Grader walk (register expected interactions now):**
  - `--no-judge` ⇒ the **narrative** dimension is not scored. item_passed for
    causal items does not read it (`report.py`), so pass^k is unaffected; the
    arm simply does not speak to narrative quality. Registered, not a surprise.
  - **thinking asymmetry is real and intended.** haiku runs thinking-off (the
    model has no thinking mode); Opus runs thinking-adaptive. This is a property
    *of the models*, not a harness artifact — the arm measures haiku as it
    actually runs. Report it alongside the numbers, do not "fix" it.
  - **discipline** dimension: haiku's uncached re-read fraction feeds it (the
    pilot's 1.5b tripped it). Expected to pass for a competent model; if it
    fails broadly, that is a real haiku finding, not an instrument fault.

## Assumptions

| # | Assumption | How it was checked before running |
|---|---|---|
| 1 | The store is per-item scratch, not shared real data | `runner.py:115` wipe + preflight `runner.py:260` node cap; pilot ran green |
| 2 | The full arm's fingerprint matches the Opus reference | Assert single `cache_fingerprint == b041069e` on both run files before `arms` |
| 3 | haiku is priced, so `--max-cost` is a live brake | `PRICES_PER_MTOK["claude-haiku-4-5"] = (1.0, 5.0)`, `runner.py` |
| 4 | ~$2 estimate | 90 rows × $0.0222/item (pilot, cache-creation included); brake at $6 |
| 5 | Same 30-item set as the Opus arm | both run `evals/scenarios/base.jsonl` (30 lines); `arms` declines otherwise |

## The command (after review, on go)

```
uv run resgraph-evals run --worker haiku --no-judge --trials 3 \
  --scenarios evals/scenarios/base.jsonl \
  --out-dir evals/runs \
  --max-cost 6.0 --max-item-cost 0.50
```

Then, before any `arms` call: confirm 90 rows, one fingerprint, and that the
fingerprint equals the Opus reference run's. The Opus reference run (baseline
refresh + arms anchor) gets its **own** pre-flight — it is the expensive arm.
