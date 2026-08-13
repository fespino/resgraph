# Pre-mortem: the paired skill arm on Haiku (#199)

*Written before the first paid run. See the [drill runbook](README.md). A paid
run is a deploy even at ~$1.62; the causal chain and measure-nothing modes are
shared with the [model-arm pre-mortems](premortem-opus-reference.md), so only
the deltas are argued.*

**Claim under test:** the change-forensics playbook (the ~3.6k-token skill in
the analyst prefix) narrows Haiku's honesty gap — its over-attribution on
controls (control 0.17) and its fabrications (2/90). If the skill is
load-bearing, without-skill Haiku is measurably *worse* on control /
discipline / fabrications; if the gap is intrinsic, the delta is ~zero.
**Fault:** none. The premise that can silently fail is the toggle: `--no-skill`
must actually drop the playbook, which **moves the prompt fingerprint**. A run
whose fingerprint did NOT move measured the skill against itself — nothing.
**The number:** the `skill-value` ledger (available → retrieved → invoked →
relevant) and the per-slice deltas (control, discipline, fabrication count)
between the existing with-skill arm and this without-skill run.
**Estimated cost:** **~$1.62** (Haiku, 90 rows) + a ~$0.02 pilot. Brake
`--max-cost 4`, `--max-item-cost 0.30`. Resume-ready against the org cap.

## The with-skill side already exists

`evals/runs/20260813T154547Z.jsonl` (Haiku, `with_skill=true`, `--no-judge`,
k=3, fingerprint `b041069e`) is the with-skill arm. This experiment adds only
the without-skill run: `--worker haiku --no-skill --no-judge --trials 3` on the
same `evals/scenarios/base.jsonl`. `--no-judge` matches the with-skill arm so
`item_passed`/found-rate/honesty are graded identically.

## Deltas from the model-arm pre-mortems

| Failure of the experiment | Checked by |
|---|---|
| the skill was not actually dropped (fingerprint unchanged) ⇒ two identical arms | **Pilot gate** — the pilot fails if `cache_fingerprint == b041069e`; `--no-skill` MUST move it. This is the load-bearing check. |
| the two arms measure different item sets | `skill-value` compares item sets; both run the 30-item base suite. |
| judge confound | `--no-judge` on both arms (matched). |
| no tool use | assert `tool_trace`≥1 (`verify`). |
| cost cap trips | cap $4 vs ~$1.62; resume; assert 90 rows. |

**Fabrications are the SIGNAL here, not a halt.** Unlike a certification run,
`fabrication_count > 0` does not stop this experiment — the whole point is to
compare Haiku's fabrication count with vs without the skill. Both arms may
fabricate; the delta is the finding. (A certification would still halt; this is
characterization of the skill's effect.)

## Pilot

- 1 causal item (ambiguous-s42006), k=1, `--worker haiku --no-skill --no-judge`.
- **Pass condition:** parsed report, ≥1 tool call, and **`cache_fingerprint`
  ≠ `b041069e`** (the skill was dropped — the fingerprint moved). If it did not
  move, STOP: the toggle is wired wrong and the run would measure nothing.

## What decides the result

- **Measured, not decisive:** the four-stage `skill-value` ledger and the
  per-slice deltas. Focus: control / honesty / discipline / fabrication count
  (the skill's plausible effect is on method and restraint, not raw recall).
- **Registered limits (EVALS.md):** `retrieved` collapses into `available`
  (static prefix — cannot separate "in context" from "read"); `invoked` is a
  heuristic scored from the method's shape (#189). The render says both.
- **The conclusion updates the caveat:** a positive control/discipline delta
  softens SPEC D29c's "surface-for-review" caveat (the skill carries honesty);
  a ~zero delta hardens it (the gap is the model's).

## The commands (after review, on go)

```
# pilot — 1 item, ~$0.02; MUST move the fingerprint off b041069e
uv run resgraph-evals run --worker haiku --no-skill --no-judge --trials 1 \
  --scenarios <one-item pilot.jsonl> --out-dir <scratch> --max-cost 1.0 --max-item-cost 0.30

# the without-skill arm
uv run resgraph-evals run --worker haiku --no-skill --no-judge --trials 3 \
  --scenarios evals/scenarios/base.jsonl --out-dir evals/runs \
  --max-cost 4 --max-item-cost 0.30
```

Then `verify` (single fingerprint, ≠ b041069e), and
`resgraph-evals skill-value <with_skill_run> <this_run>`.
