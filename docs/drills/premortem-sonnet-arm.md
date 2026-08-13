# Pre-mortem: the Sonnet arm (full base suite, at the current fingerprint)

*Written before the first paid run. See the [drill runbook](README.md) and the
[Opus reference pre-mortem](premortem-opus-reference.md) — the causal chain,
the "measure nothing" table, and the org-cap resume mitigation are identical
(same suite, same fingerprint, same `--no-judge`); only the deltas are argued
here.*

**Claim under test:** `claude-sonnet-4-6` is the registered decision-rule arm
(#100): "Sonnet pass^k ≥ Opus − 0.07 → the production recommendation flips to
Sonnet." After the Opus/Haiku inversion, the sharper question is the **slice
profile**: does Sonnet combine Haiku's recall (transitive/found_top3) with
Opus's honesty (control/abstention, zero fabrications), or does it inherit one
model's failure mode? That is what "completes the picture."
**Fault:** none. The premise that can silently fail is comparability +
completeness — same 30 items, same fingerprint `b041069e` as the Opus and Haiku
arms, all 90 rows.
**The number:** Sonnet pass^k / pass@k, per-slice rates (control AND transitive
are the two to read), cost per passed triage, latency — the third row of the
arms table.
**Estimated cost:** **~$7.68** — the Opus run's token profile re-priced at
Sonnet rates ($3/$15 per Mtok; Sonnet also runs adaptive thinking, so the token
shape is comparable). Brake `--max-cost 12`, `--max-item-cost 0.60`. Sonnet is
priced `(3.0, 15.0)`, so the cap is armed. Resume-ready against the org cap
(fired on the Opus arm — the pre-mortem's realized risk).

## Deltas from the Opus pre-mortem

- **Worker** `--worker sonnet` → `claude-sonnet-4-6`, adaptive thinking ON
  (its `workers.yaml` setup carries `extra_args.thinking`, like Opus).
- **Not a baseline change.** Sonnet ≠ the baseline's worker (`claude-opus-4-8`),
  so the worker-aware gate (#195, D29c) SKIPS it as an `arms` comparison — no
  `eval-baseline-refresh` label, no gate risk from committing it (unlike the
  Opus arm, which was the baseline). It is a labeled arm, like Haiku.
- **The decision rule reads differently now.** The registered rule was
  Sonnet-vs-Opus on pass^k; but Opus scored 0.60 and does not dominate Haiku
  (0.63), so a bare pass^k rule is thin. Register the real read here: the arm
  is decisive if Sonnet's **slice profile** is unambiguous — recall like Haiku
  (transitive ≥ ~0.8, found_top3 ≥ ~0.85) AND honesty like Opus (control ≥
  ~0.7, fabrications 0). A middling profile (recall and honesty both mediocre)
  is also a finding: the harness's capability floor is real and Sonnet sits on
  it.

## How could this measure nothing? (deltas only; full table in the Opus pre-mortem)

Identical modes, identically checked: stale world → per-item wipe
(`runner.py:115`); fingerprint ≠ `b041069e` → pre-run assertion; different item
set → `arms` declines; cap trips → resume, assert 90 rows; no tools → assert
`tool_trace`≥1; judge confound → `--no-judge`. The verification is now a
command: **`resgraph-evals verify <run> --rows 90 --fingerprint b041069e
--items 30 --min-trials 3`** must exit 0 before the arm is fed to `arms`.

## Pilot

- 1 causal item (ambiguous-s42006), k=1, `--worker sonnet --no-judge`. ~$0.08.
- **Pass condition:** parsed report, ≥1 tool call, `cache_fingerprint`
  `b041069e`, cost in band. Confirms the Sonnet setup (thinking on, b041069e)
  before the full spend.

## What decides the result

- **Halt:** `fabrication_count > 0` → not a clean arm (characterization only),
  same rule as Haiku/Opus.
- **Measured, not decisive:** pass^k, the two key slices (control, transitive),
  cost per passed triage, latency.
- **The three-way read:** the point is not Sonnet in isolation but where it
  lands on the commit↔abstain axis between Haiku (over-commits) and Opus
  (under-commits). Report the arms table for all three, and the control ×
  transitive scatter — that is the completed picture.
- **thinking asymmetry stands:** Sonnet and Opus run thinking-on; Haiku ran
  thinking-off (no thinking mode). Each model as it would be deployed.

## The commands (after review, on go)

```
# pilot — 1 item, ~$0.08
uv run resgraph-evals run --worker sonnet --no-judge --trials 1 \
  --scenarios <one-item pilot.jsonl> --out-dir <scratch> --max-cost 1.0 --max-item-cost 1.0

# the arm — full suite, resume-ready
uv run resgraph-evals run --worker sonnet --no-judge --trials 3 \
  --scenarios evals/scenarios/base.jsonl --out-dir evals/runs \
  --max-cost 12 --max-item-cost 0.60
# if truncated:  --resume evals/runs/<that file>
```

Then `verify` (exit 0), and `arms opus=… haiku=… sonnet=…` for the three-way table.
