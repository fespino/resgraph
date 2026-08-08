# Pre-mortem: analyst degraded-honesty drill

*Written 2026-08-07, after two runs of the original design measured nothing ([INC-002](../incidents/INC-002-degraded-drill-misfire.md), $5.88). This is the document that should have existed before the first one. It is kept as written, including the row that would have stopped both runs, because a template only earns its place if it catches the thing it was made for.*

**Claim under test:** the analyst degrades honestly — when a store it depends on dies mid-triage, it finishes with what it can still reach and says what it lost.
**Fault:** a store handle raises after N acquisitions; the other store keeps answering.
**The number:** found-rate on degraded runs vs normal — the cost of honest degradation, currently rounded to zero because nobody has measured it.
**Estimated cost:** ~$3.15 for 7 items × k=3, from the certified run's own $0.15/run across 90 rows. (The original registration said ~$1. It was wrong by 3×, corrected before the run in `304b660`.)

## Causal chain

| # | Link | Evidence |
|---|---|---|
| 1 | fault raises on store acquisition | `evals/faults.py:29` |
| 2 | a tool that reads that store fails | see the workload check below |
| 3 | the failure reaches the agent as a tool error | `analyst/tools.py:100` — any exception becomes `ok=False` |
| 4 | the report can express the loss | `analyst/models.py` — `TriageReport.degraded` |
| 5 | the graded output records it | `evals/graders.py:92` — the `degraded` dimension |

**Workload check — the row that invalidates the original design.** Link 2 does *not* hold for the hot store on this workload:

```
grep -rn 'require(' src/resgraph/tools/canonical/     # 5 hits, the whole surface

traversal.py:84   require("hot")    dependency_path      — always hot
entity.py:35      require("hot")    fetch_resource       — ONLY when at is None
entity.py:48      require("cold")   fetch_resource       — when at is set
history.py:42,76  require("cold")   resource_history, world_diff
```

Triage investigates a **past** alert, so the agent passes `at=<fired_at>` and `fetch_resource`/`blast_radius` read cold. The only unconditionally-hot tool is `dependency_path`: **1 call in 132** across a real run. Killing the hot store removes a capability the agent was barely using.

**Consequence: the fault must target the cold store, or both.** The cold store is what a time-travel triage actually depends on. A hot-store kill is a different, much weaker experiment, and it is the one that has already been run twice for nothing.

## How could this run complete, produce numbers, and measure nothing?

| Failure of the experiment | Checked by |
|---|---|
| the fault never reaches the subject | the `degraded` dimension **fails** an item with no failed tool call, and the pilot gate below aborts before the suite — this is what caught INC-002 twice |
| the subject does not depend on what was broken | the workload check above; for the hot store the answer was "it does not", which is why the design changed |
| the fault fires but the report cannot express it | `TriageReport.degraded` exists and is graded; link 4 is cited |
| the numbers are computable from runs where nothing happened | they are — `found_top3` computes fine on a void run. Only the fault-fired assertion prevents publishing it |
| the fault fires so early the run is trivially blind | `DEGRADED_KILL_AFTER >= 2` is asserted in the suite; killing on call 1 tests the error path, not the behavior |

## Pilot

- **Smallest falsifying case:** one item, k=1, ~$0.15, written into `scripts/drill-analyst-degraded.sh` as a gate.
- **Pass condition:** at least one `tool_trace` entry with `ok: false`.
- **If the pilot fails:** the script exits non-zero and refuses the suite. With the current hot-store fault it *will* fail, correctly, and that is the honest state of the drill today.

## What decides the result

- **Halt condition:** fabrications > 0 after the kill. Not a datapoint — the failure mode the drill exists to catch.
- **Decisive:** the `degraded` dimension, per item at k=3.
- **Measured, not decisive:** `found_top3` degraded vs the certified 0.792. No threshold; letting it decide would grade the agent on reaching a cause the dead store was holding.
- **Not a gate candidate:** its own dataset and its own slice, so it never enters a comparison against the base baseline — the gate declines mismatched item sets by construction (D29b — agent SLOs and the CI eval gate).

## Assumptions

| # | Assumption | How it was checked before running |
|---|---|---|
| 1 | the cold store is what this workload depends on | the `require()` audit above, plus 47 `resource_history` + 21 `world_diff` calls in 132 |
| 2 | killing it produces observable degradation | **unchecked until the pilot** — this is exactly the assumption that was wrong last time, in mirror image |
| 3 | the agent can still answer partially with the hot store alone | unchecked; a live-only triage of a past alert may be impossible, in which case the honest result is total blindness, and the graded question becomes whether it says so |
| 4 | the agent does not fabricate under partial failure | prior evidence is weak — fabrications 0 across two runs where almost nothing failed |
| 5 | k=3 on 7 items is enough to verdict per item | flap floor (#137) applies: below k=3 the gate declines, and so should we |

Assumptions 2 and 3 are the ones to watch. They are load-bearing, currently unchecked, and the pilot exists to check the first before any money is spent on the second.
