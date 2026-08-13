# Pre-mortem: the Opus reference arm (full base suite, at the current fingerprint)

*Written before the first paid run. See the [drill runbook](README.md). Not a
fault drill — a paid run is a deploy (#100 model arms, #192/D29c), and this is
the expensive one (~$13.50), so the pre-flight is not optional. Companion to
[the Haiku arm pre-mortem](premortem-haiku-arm.md); only the deltas are argued
at length.*

**Claim under test:** `claude-opus-4-8`, run as the reference worker at the
*current* instruments (fingerprint `b041069e`), anchors the arms table — Haiku's
0.633 pass^k and its control collapse mean nothing without a matched Opus
number — and this same run refreshes the D29b baseline, which is stale
(committed at `84d04e11`, drifted via #106).
**Fault:** none induced. The premise that can silently fail is **comparability +
completeness**: same 30 items, same fingerprint as the Haiku arm, all 90 rows.
**The number:** Opus pass^k / pass@k, per-slice found rates (esp. **control**,
where Haiku scored 0.167), cost per passed triage, latency p50/p95 — the Opus
row of the arms table, and the new baseline.
**Estimated cost:** **~$13.50 worker** — the *actual* cost of the last full Opus
k=3 on this suite (`20260803T221121Z`, re-priced from its rows). Dominated by
adaptive-thinking output (365k output tokens) + cache; input negligible
(prefix caches). Brake at `--max-cost 20`, `--max-item-cost 1.0`. `--no-judge`,
so no judge spend.

## Causal chain

Identical to the Haiku arm except the worker; only the differing links restated.

| # | Link | Evidence |
|---|---|---|
| 1 | `--worker opus` → Anthropic client, `claude-opus-4-8`, **adaptive thinking ON** | `cli.py` load_setup/build_client; the opus setup carries `extra_args.thinking: {type: adaptive}` (#196) |
| 2 | world rebuilt from seed, loaded fresh per item | `runner.py:115` wipe + load_snapshot + apply_batch |
| 3 | Opus investigates with tools, emits a hedge report | `harness.py:216`/`:289` create |
| 4 | graders score vs the planted cause; item_passed is deterministic | `report.py:26` (judge-independent — narrative is a separate dim) |
| 5 | row records model, tool_trace, dims, tokens, latency, fingerprint | `runner.py:414`/`:489` |
| 6 | `arms` compares vs Haiku **only if item sets match**; the run refreshes the baseline only if complete | `arms.py:56` item_ids equality; aggregate over 90 rows |

**Workload check:** links hold — this is the certified worker running the suite
it was certified on. Capability is not in question; *completion* and
*fingerprint match* are.

## How could this run complete, produce numbers, and measure nothing?

| Failure of the experiment | Checked by |
|---|---|
| **org spend cap truncates the run** (fired ≥3× in the certification history: 19/90, 17/30) | **`--resume`** — a truncated run resumes to 90 rows; a partial is NOT fed to `arms` (item-set mismatch) and NOT aggregated as the baseline. Assert 90 rows before use. |
| the Opus fingerprint ≠ the Haiku arm's `b041069e` ⇒ arms confounds harness with model | **Pre-run assertion** — single `cache_fingerprint`, must equal `b041069e`. Thinking does not affect the prefix hash; with-skill + same base.jsonl ⇒ same prefix. |
| different item set than Haiku ⇒ invalid comparison | **`arms` declines** (`arms.py:56`); both run `evals/scenarios/base.jsonl` (30). |
| `--max-cost` trips mid-run ⇒ partial scored as whole | **Cap at $20 vs ~$13.50 expected** (~1.5× headroom); if it trips, resume — do not aggregate the partial. |
| Opus never really used tools | **Assert** `tool_trace`≥1 per row (opus is the certified worker; a floor check, not a doubt). |
| judge cost/confound | **Removed** — `--no-judge`, matching the Haiku arm. pass^k is judge-independent; the narrative dimension is out of scope for both arms (registered deviation from the model-arms registration, which had assumed a pinned judge). |
| **comparing against the stale baseline by reflex** | The committed baseline is `84d04e11`; do NOT diff these `b041069e` numbers against it. This run *becomes* the b041069e baseline; the Haiku arm is the comparand, via `arms`. |

Nothing reads "nothing".

## Pilot

- **Smallest falsifying case:** 1 causal item (ambiguous-s42006), k=1,
  `--no-judge`, `--worker opus`. ~$0.15–0.30.
- **Pass condition:** a parsed report, ≥1 tool call, `cache_fingerprint ==
  b041069e`, cost in the expected per-item band (~$0.15). This confirms the
  *current config* (post-#196 opus setup, adaptive thinking, b041069e) produces
  a sane row before committing $13.50 — the org-cap history makes the pilot
  cheap insurance, not ceremony.
- **If the pilot fails** (wrong fingerprint, no tools, cost 10× off): stop.

## What decides the result

- **Halt conditions:** `fabrication_count > 0` on aggregate stops the "clean
  baseline refresh" claim (same unconditional rule as the gate). For the *arms
  characterization* it is a finding, not a stop — but a fabricating reference is
  a baseline problem, so a non-zero count is escalated, not absorbed.
- **Measured, not decisive:** pass^k, pass@k, per-slice rates (control the
  headline vs Haiku), cost per passed triage, latency percentiles.
- **Baseline refresh is a follow-on, not automatic:** aggregating this run into
  `evals/baseline.json` is a separate, labeled step (the `eval-baseline-refresh`
  discipline), taken only after 90 rows verify and fabrications are understood.
- **Grader walk:**
  - `--no-judge` ⇒ **narrative** unscored on both arms; item_passed unaffected.
  - **thinking asymmetry is intended** — Opus runs adaptive thinking (native),
    Haiku ran thinking-off (no thinking mode). The arms compare each model
    as it would actually be deployed; report it, do not "normalize" it.
  - **control** is the slice to read first: Haiku 0.167. Opus is expected to
    abstain far better; the *size* of that gap is the phase's economic answer.

## Assumptions

| # | Assumption | How it was checked before running |
|---|---|---|
| 1 | Per-item scratch store, not shared data | `runner.py:115` wipe + preflight `runner.py:260`; the Haiku arm ran it green today |
| 2 | Fingerprint matches the Haiku arm | Assert single `cache_fingerprint == b041069e` on both files before `arms` |
| 3 | ~$13.50 estimate | actual cost of `20260803T221121Z` (90-row Opus k=3, same suite), re-priced from its rows |
| 4 | Opus is priced; `--max-cost` is a live brake | `PRICES_PER_MTOK["claude-opus-4-8"] = (5.0, 25.0)` |
| 5 | Same 30-item set as Haiku | both run `evals/scenarios/base.jsonl`; `arms` declines otherwise |
| 6 | A truncated run is recoverable | `--resume` is supported and tested; the certification survived org-cap truncation this way |

## The commands (after review, on go)

Pilot first, then the suite:

```
# 1) pilot — 1 item, ~$0.15, confirms config + fingerprint before the big spend
uv run resgraph-evals run --worker opus --no-judge --trials 1 \
  --scenarios <one-item pilot.jsonl> --out-dir <scratch> \
  --max-cost 1.0 --max-item-cost 1.0

# 2) the reference arm — full suite, resume-ready against the org cap
uv run resgraph-evals run --worker opus --no-judge --trials 3 \
  --scenarios evals/scenarios/base.jsonl --out-dir evals/runs \
  --max-cost 20 --max-item-cost 1.0
# if truncated:  --resume evals/runs/<that file>
```

Then: confirm 90 rows, one fingerprint == `b041069e`, `tool_trace`≥1/row,
`fabrication_count`; and `arms opus=… haiku=…` for the table.
