# INC-NNN: <what happened> (induced)

**Status:** <resolved / remediated / redesign pending> · **Induced:** yes (`scripts/<drill>.sh`, run YYYY-MM-DD)
**Impact:** <what it cost, what it affected — say "no production impact" plainly if the subject is the instrument>
**Evidence:** <committed run files, pre-registration, pre-mortem>
**Code under test:** <git ref per attempt; run rows carry `git_ref`, so cite the hash and skip the tag>

## What was supposed to happen

The claim, and the number the drill set out to produce.

## What happened

| T (UTC) | Event |
|---|---|
| | |

The headline numbers, stated before they are interpreted.

## Diagnosis

If the first diagnosis was wrong, **keep it, label it, and say why it was believed** — it usually explained the evidence, came from real code, and predicted a fix. What it lacked was a test of whether the cause it named was the cause operating. That gap is the useful part of the note.

Then: the diagnosis that survived, and what evidence distinguished it from the first.

## What is established, and how strongly

Separate results that survive from results that only look like they do. A number computed correctly from runs where nothing happened is not a finding — say so where it appears, not in a caveat at the end.

## Assumptions, audited

| # | Assumption | Verdict | Why |
|---|---|---|---|
| 1 | | Right / Wrong / Right but weakly tested | |

Mark the ones that were never checked before the run. Those are usually the wrong ones.

## What we should have done differently

Ordered by what it would have cost to do. Cheapest first is usually most damning.

## Remediation

- **Kept:** <changes that are independently correct>
- **Superseded:** <changes made on a diagnosis that turned out wrong — say which>
- **Open:** <what is still undecided, and why it is a design decision rather than a re-run>

## What generalizes

The lessons that outlive this system.

## Action items

- [ ] …
