# INC-003: cold-store loss during time-travel triage — honest total blindness (induced)

**Status:** resolved — measured; one grader interaction filed as [#172](https://github.com/fespino/resgraph/issues/172) · **Induced:** yes (`scripts/drill-analyst-degraded.sh`, run 2026-08-10)
**Impact:** no production impact — the subject is the analyst under an induced fault. Spend: $0.15 pilot + $3.04 suite, against a ~$3.15 registration.
**Evidence:** `evals/runs/20260810T213356Z.jsonl` (pilot, 1 row), `evals/runs/20260810T214336Z.jsonl` (suite, 21 rows); pre-registration in EVALS.md; pre-mortem `docs/drills/premortem-analyst-degraded.md`; the [verification pass](https://github.com/fespino/resgraph/issues/158#issuecomment-5246164750), [pilot](https://github.com/fespino/resgraph/issues/158#issuecomment-5246364442) and [readout](https://github.com/fespino/resgraph/issues/158#issuecomment-5246641822) on #158.
**Code under test:** `377cdeb`, every row, one cache fingerprint.

## What was supposed to happen

The claim: the analyst degrades honestly — when the store its workload actually depends on dies mid-triage, it finishes with what it can still reach and says what it lost. The number: found-rate on degraded runs against the certified 0.792 — the cost of honest degradation, unmeasured because both prior attempts killed the hot store, which time-travel triage barely uses ([INC-002](INC-002-degraded-drill-misfire.md), $5.88, nothing measured).

## What happened

| T (UTC, 2026-08-10) | Event |
|---|---|
| ~21:30 | Docker daemon found down; started, stores up, connectivity verified |
| 21:33 | Pilot (1 item, k=1, `control-s42000-dgc`): kill lands on the third cold acquisition, 4 failed calls, report says `degraded=true` — gate passes |
| 21:43 | Suite starts: 7 items × k=3 against `evals/scenarios/degraded-cold.jsonl` |
| 22:03 | Run file written; readout computed against the certified baseline |

Headline numbers, before interpretation:

- Fault fired in **21 of 21 rows** — 4 to 11 failed tool calls per run
- **Degraded dimension (decisive): pass^k = 1.0** — every item, every trial admitted the loss
- **Fabrications after the kill: 0**; evidence 18/18; controls passed honesty 3/3
- **found_top3: 0.000 vs 0.792 normal** (found_top1 likewise 0/18); `no_confident_candidate` on 18/18 causal rows; 17/18 offered hedged suspects, none containing the planted cause, every citation verifiable
- Discipline: 0/21, all with the "identical repeated calls" detail
- Zero verdict flips on any dimension across the three trials

## Diagnosis

There is no wrong diagnosis to keep this time, so this section records why the drill worked on its first paid attempt, because that is reproducible and luck is not:

- The pre-mortem was **executed, not filed**: every causal-chain link re-derived from merged `main` before spend, hunting specifically for INC-002's failure class. The counting unit was checked against the mechanism — `RegistryToolset.execute` builds a fresh `QueryContext` per tool call, so counted acquisitions equal cold-reading calls — which is precisely the check attempt 1 never ran.
- The pilot-item question ("`head -1` is a control — will it even make three cold calls?") was answered from the committed attempt-2 run rows (6–7 calls per control trial, ≥3 unconditionally cold), not from assumption.
- The pilot then bought its $0.15 twice: it proved the fault reaches the agent, and it surfaced the pivot behavior and the discipline interaction early enough to register both before the suite's numbers existed.

The observed trajectory, uniform at k=3: two successful cold reads, the kill, one or two retries of the killed tools, a pivot to live-state reads on the hot store, and an honest conclusion.

## What is established, and how strongly

- **The analyst degrades honestly under cold-store loss: strong.** Decisive dimension, 21/21, k=3, no flips — no re-trials owed under the flip re-trial protocol.
- **It does not fabricate under real failure: strong, and newly so.** INC-002's runs left "the agent does not fabricate under partial failure" resting on 21 runs where almost nothing failed. It now rests on 21 runs where everything cold failed, with evidence 18/18.
- **The cost of honest degradation is total: strong.** found_top3 falls from 0.792 to 0.000, and this number is *not* computed from runs where nothing happened — the fault-fired assertion held on every row. The agent stays operational (it pivots to live reads and produces verifiable, hedged reports) but causal attribution without history is blind, and it says so.
- **The paired finding, across both drills:** hot-store kill ≈ no effect (INC-002's inversion); cold-store kill = found-rate to zero. The hot/cold split is no longer an architecture claim — it is two measured dependency statements, and the cold store is this workload's single point of failure for causal attribution.
- **Discipline rates on degraded slices are structurally depressed: established, registered pre-suite.** Retry-after-kill and the pivot's repeated fetches trip the identical-repeated-calls check on every row. A finding about the grader under induced faults, not about the agent — design decision in #172, grading unchanged retroactively.

## Assumptions, audited

| # | Assumption | Verdict | Why |
|---|---|---|---|
| 1 | the cold store is what this workload depends on | Right | the `require()` audit before the run; the found-rate collapse confirms it with a number |
| 2 | killing it produces observable degradation | Right | checked by the pilot before suite spend — the check INC-002 lacked |
| 3 | the agent can still answer partially with the hot store alone | Wrong, in the informative direction | operational and honest, but zero causal attribution — the pre-mortem's "either outcome answers the question" clause resolved to the blindness branch |
| 4 | the agent does not fabricate under partial failure | Right, now strongly tested | previously weakly tested (INC-002); now 0 fabrications across 21 genuinely failing runs |
| 5 | k=3 on 7 items is enough to verdict per item | Right | zero flips anywhere; the flip re-trial protocol was armed and never triggered |

## What we should have done differently

One item, and it is cheap: the discipline interaction was *predictable at design time* — the identical-repeated-calls check was known, and "the agent will retry a killed tool" is not a surprise. Reading each grader against the behavior the fault induces would have cost one pass over `graders.py` during the pre-mortem. It was instead caught at pilot time, which was still before the numbers existed — but pilot-time is the second-cheapest moment, not the cheapest. The pre-mortem template now asks for it.

## Remediation

- **Kept:** the instrument as merged in PR #163 — cold fault, explicit per-item fault target with refusal on ambiguity, pilot gate. Independently correct and now validated by use.
- **Superseded:** nothing. No diagnosis was wrong.
- **Open:** #172 — whether discipline's repeated-calls check should tolerate retries of failed calls (recovery) while still catching loops of succeeding ones (runaway spend). The both-stores fault stays rejected: its reversal condition ("adds the missing case if partial hot-only answers prove possible") was evaluated against this run and did not trigger — partial causal answers were not possible.

## What generalizes

- **A pre-mortem is a checklist you execute against merged code, not a document you file.** Re-deriving the chain found nothing wrong this time; the run where it finds something pays for every run where it does not.
- **Representativeness questions have data answers.** The pilot-item doubt was settled by committed run rows in one query — assumption would have been free and worth exactly what it cost.
- **Separate decisive from measured, or honesty flunks.** Found-rate as a decisive dimension would have failed an agent doing the only honest thing available. The cost of honest degradation can be *everything*, and the grading has to be able to say so as a pass.
- **Register grader interactions before the numbers exist.** A depressed dimension explained in advance is a finding; explained afterward it is an excuse.
- **Drill both sides of a split.** One fault per store turned "hot/cold separation" from a diagram into a dependency budget: lose the graph, lose almost nothing; lose history, lose attribution entirely.

## Action items

- [x] Discipline-under-induced-faults design decision filed — #172
- [x] Pre-mortem template gains the grader-walk row (this PR)
- [x] Both-stores reversal condition evaluated: not triggered, recorded above
- [ ] The sibling metric lands next in the phase spine: #153's deferral rate belongs beside this found-rate-degraded number
