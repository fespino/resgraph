# Pre-mortem: <drill name>

*Written before the first paid run. See the [drill runbook](README.md).*

**Claim under test:** <the sentence the system asserts about itself>
**Fault:** <what is induced, where it is injected>
**The number:** <what this run produces that did not exist before>
**Estimated cost:** <$X, and the per-run figure it comes from>

## Causal chain

Every arrow cited. The one you cannot cite is the one that breaks.

| # | Link | Evidence |
|---|---|---|
| 1 | fault is injected | `file.py:NN` |
| 2 | a component that depends on it fails | `file.py:NN` |
| 3 | the subject observes the failure | `file.py:NN` |
| 4 | the observation reaches the graded output | `file.py:NN` |

**Workload check:** does link 2 hold for the workload actually being run, or only in general? <answer>

## How could this run complete, produce numbers, and measure nothing?

List every way. For each, say whether it is checked and by what.

| Failure of the experiment | Checked by |
|---|---|
| the fault never reaches the subject | <grader dimension / pilot / nothing — say so> |
| the subject does not depend on what was broken | <…> |
| the fault fires but the graded output cannot express it | <…> |
| the numbers are computable from runs where nothing happened | <…> |

Anything in the right-hand column reading "nothing" is a reason not to run yet.

## Pilot

- **Smallest falsifying case:** <one item, k=1, ~$0.15>
- **Pass condition:** <e.g. at least one failed tool call in `tool_trace`>
- **If the pilot fails:** stop. Do not spend on the suite.

## What decides the result

- **Halt conditions** (a result that stops everything): <e.g. fabrications > 0>
- **Measured, not decisive:** <numbers reported without a threshold>
- **Not a gate candidate:** <why this run does or does not enter the eval gate>

## Assumptions

Listed now so the postmortem can audit them.

| # | Assumption | How it was checked before running |
|---|---|---|
| 1 | | |
