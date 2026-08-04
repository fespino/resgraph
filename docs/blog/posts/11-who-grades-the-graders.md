---
date: 2026-08-04
categories:
  - AI agents
tags:
  - evals
  - ai-assisted-coding
  - mutation-testing
  - llm-judge
  - cost
  - honesty
---

# Who grades the graders?

The previous two posts told the story of an agent being measured:
a triage agent graded against planted ground truth, a prompt
gamed and a structure that fixed it, every claim carrying a run ID.
Here is the question underneath that story: the instrument doing
the measuring — the graders, the scenario generator, the mutation
suite — is AI-generated code. I set the framework and the controls:
the quality bar, the taxonomy's shares, the halt conditions, what
merges. The volume under that framework — the planted cases, the
grader implementations, the invariants and post-conditions, the
thirteen mutants — was generated, under review, by the same kind of
system the harness exists to grade. I think that split is not a
quirk of this project but the coming shape of the work. Evals
measurably erode within a single model generation
([Anthropic's numbers](https://www.anthropic.com/engineering/AI-resistant-technical-evaluations)),
synthesized harnesses already outperform hand-configured ones in
controlled settings
([AutoHarness](https://arxiv.org/abs/2603.03329)), and large
benchmarks already scale by LLM-synthesizing their tasks
([STATE-Bench](https://github.com/microsoft/STATE-Bench), which
discloses it). The strongest generate-and-evaluate system published
so far, [AlphaEvolve](https://arxiv.org/abs/2506.13131), runs
exactly this split — humans define an automated evaluator, the AI
evolves the code volume against it — and names machine-gradability
as its main limitation. Harnesses will increasingly be regenerated
from specs, behavior, and production data — humans owning the
framework and controls, AI doing the case-picking and
invariant-writing where it is simply more efficient. Which makes
the question I stopped to ask mid-phase the general one, not the
local one: **how do I know the evals are correct?** This post is
the answer I can defend — four layers of
argument, two checks you can run yourself, a list of my own
mistakes with dollar costs attached, and the full price of the
program.

<!-- more -->

This post is about **resgraph**, a mini referential data platform
built in public, and it closes the trio that began with
[the eval build](09-ground-truth-first-judge-last.md) and
[the honesty arc](10-goodhart-inside-a-prompt.md). It is also the
post where the trio's subject inverts: the first two audited the
agent; this one audits the instrument — and the discipline it lands
on is the one any AI-generated harness will need, because this
build is an early instance of a pattern that is about to be
everywhere.

One thing the background reading surfaced is worth naming up front,
because it shapes the whole post: the two bodies of literature this
question needs do not cite each other. The synthesis side shows
harness layers being generated —
[AutoHarness](https://arxiv.org/abs/2603.03329),
[AlphaEvolve](https://arxiv.org/abs/2506.13131),
[Code World Models](https://arxiv.org/abs/2510.04542) — yet
AutoHarness's bibliography, checked reference by reference, contains
no eval-integrity work at all: nothing on
[eval erosion](https://www.anthropic.com/engineering/AI-resistant-technical-evaluations),
on [subjects detecting and gaming the test](https://www.anthropic.com/engineering/eval-awareness-browsecomp),
on [lucky passes](https://arxiv.org/abs/2605.12925) (up to 23.2% of
agent passes in one audit), or on
[the environment confounding the score](https://www.anthropic.com/engineering/infrastructure-noise).
The integrity side documents exactly the failure modes a generated
harness would inherit. The junction is only now being built:
[Agentic Harness Engineering](https://arxiv.org/abs/2604.25850)
evolves harnesses autonomously from trajectory data and pairs every
edit with a falsifiable prediction checked against the next round —
pre-registration as machinery. But nothing in that loop audits its
own graders, and the bar it optimizes is the benchmark itself. Who
sets the bar, and who checks the checker, is still left to the
humans — and the four layers, the mutation audit, and the mistakes
ledger below are this project's working answer for that remainder.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-8-analyst`](https://github.com/fespino/resgraph/tree/phase-8-analyst).
    The audit trail this post summarizes is
    [`EVALS.md`](https://github.com/fespino/resgraph/blob/phase-8-analyst/EVALS.md).

## Four layers, in strength order

**Layer 1: the graders mostly aren't AI.** Four of the five grading
dimensions are deterministic code — string and set comparisons
against ground truth that is *construction, not judgment*. The
scenario generator knows the planted cause because code planted it;
"did the agent find it?" is a comparison, not an opinion. An AI
wrote that code, but no AI runs at grading time, which reduces the
question from "do I trust a model's judgment?" to "is this code
correct?" — roughly a hundred readable lines whose job is to
compare identifiers. That is an ordinary code-review problem, and
ordinary code-review tools apply.

**Layer 2: the correctness checks don't trust their author.** The
evidence grader — the one that decides whether a cited mechanism
edge really existed at incident time — does not implement its own
graph logic. It reuses the production as-of query, the same code
path the platform's API serves, which an earlier phase validated
with a cross-store golden test: two databases that share no query
code are required to return identical answers for the same
questions. The generator and the grader are pinned to agree through
two independently tested paths. A bug would have to survive all of
them, coherently, in the same direction.

**Layer 3: behavioral independence — the record of dissent.** An
eval built to flatter its builder does not produce a log this
embarrassing to its builder. Run after run, the instrument: halted
the baseline on the author's own prompt contract (the orientation
fabrications); refused to pass a cache target the author had
committed, exposing it as structurally unreachable from its own
token columns; invalidated five of the author's pre-registered
hypotheses, with the invalidating conditions written down before
each run; caught the model gaming a rule while the grader held; and
had its one instrument error (that cache floor) corrected through a
dated amendment rather than a quiet edit. Anthropic's
[demystifying-evals guide](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
treats instrument error as a first-class eval failure mode — the
question is never whether your instrument has errors, it is whether
the errors surface and get corrected on the record.

**Layer 4: what rests on trust is named, not hidden.** Three things
still depend on judgment. The LLM judge — deliberately the smallest
weight, grading prose coherence only, pinned to a model and frozen
template; Hamel Husain's
[evals essay](https://hamel.dev/blog/posts/evals/) argues a judge
needs its own evaluation against human agreement, and this judge
has a boundary test but not yet a human-agreement baseline (filed,
costed, on the backlog). The scenario taxonomy — designed by the
same author whose agent it tests, so its blind spots are
author-shaped; re-skins mitigate memorization, a declared coverage
statement in the discovery memo names what the taxonomy deliberately
excludes, and only an external audit removes this class of risk.
And one open grader question — whether a cited "causal change" may
ever be a sequence-0 snapshot row — sits undecided in the log
instead of silently resolved. Trust that is named has a boundary;
trust that is implicit has none.

## Two checks a reader can run

The four layers are arguments. These are executables.

**Rebuild any run row.** Every scenario is generated from a seed,
and the generator is deterministic by construction. Take any row in
any run log, regenerate its world with the CLI, and check the
planted cause against what the agent was graded on. The ground
truth is not an assertion in a JSON file; it is a reproducible
computation.

**Mutation-test the graders.** "The tests pass" establishes that
the tests *accompany* the graders, not that they *constrain* them —
a suite can ride along without gripping anything.
[Mutation testing](https://en.wikipedia.org/wiki/Mutation_testing)
measures the grip directly: break each grader on purpose and
require the suite to notice. Practitioners keep arriving here from
other directions — Birgitta Böckeler's
[sensor work for coding agents](https://martinfowler.com/articles/sensors-for-coding-agents.html)
reaches for mutation testing because "coverage tells us that a line
was executed, but not that its impact was verified."
The phase's audit used thirteen
targeted semantic mutants — not random token flips, but the
specific bug each grader exists to prevent: the found comparison
inverted, the top-3 window removed, the evidence edge-orientation
flipped (the exact bug class from the baseline), edge checking
disabled outright, the honesty conjunction weakened AND→OR, pass^k
silently computed as pass@k, controls made to always pass, the
judge's pass boundary moved by one, and five more. Targeted mutants
beat random ones for this job because every survivor is directly
interpretable as a missing test case.

First pass: **11 of 13 killed — and both survivors were real test
gaps**, not artifacts. No test pinned the inconsistent state of a
true flag alongside a high-confidence suspect, so weakening the
honesty conjunction passed; no test exercised the judge's boundary
score exactly, so an off-by-one passed. Both became tests in the
same commit; the re-run killed 13 of 13. The audit finding holes is
what distinguishes it from a ritual — and it is now a standing CI
gate (`evals/meta/mutate_graders.py`), re-run after any grader
change, exiting nonzero on survivors, so a grader edit that weakens
a test's grip cannot merge quietly.

## Our mistakes, costed

The iteration log records the model's failures; recording only
those would be auditing the subject and not the auditor. The
phase's own errors, from the honest-review section of the log, with
what each one cost:

- **Contracts committed before touching reality.** The judge was
  pinned at temperature 0 with a fixed seed; the API rejects the
  first and never offered the second — caught by the first real
  call, *after* the spec was written. Second occurrence of this
  class in the project. Now protocol: one real call before any pin.
- **A metric target committed without dry-running its formula.**
  The 0.9 cache floor was pencil-and-paper unreachable; we paid
  about $8 of runs to learn what a ten-minute simulation would have
  shown. Now protocol: simulate the formula on a hand-built trace
  first.
- **A predictably gameable rule shipped with the warning already on
  our shelf.** Iteration 3 keyed the flag to a label the model
  controls *after* we had read Anthropic's
  [AI-resistant evaluations](https://www.anthropic.com/engineering/AI-resistant-technical-evaluations)
  on measures collapsing under optimization pressure. Absorbed
  knowledge is not applied knowledge. Now protocol: the adversarial
  pre-mortem sentence in every pre-registration.
- **A known failure repeated without new evidence.** Iteration 4
  stacked a second rule directly onto a gamed one — the escalation
  ran one iteration late, at about $3.40.
- **Systematically over-optimistic predictions for prompt fixes**
  (predicted ≥ 0.83, delivered 0.33; predicted ≥ 0.67, delivered
  0.33). Now protocol: predictions in a failed technique class
  shrink toward the observed effect, and two misses escalate.
- **Operational failures that cost real money:** a first paid run
  launched against an unchecked store (it OOM'd and took the
  container runtime with it); shell pipes masking exit codes on
  money-spending commands — three runs reported success after
  crashing, a *repeat* offense already recorded as a lesson once;
  no resume in the runner, so two network drops and a billing cap
  each restarted paid work from scenario one (~$5); no
  spend-headroom check, so the org's monthly cap fired mid-run as a
  surprise.

The meta-pattern is one sentence: **knowledge that was recorded but
never promoted to an enforcing owner.** Every operational failure
above had been "learned" in prose before it happened — which is
precisely the lesson the honesty arc taught about the model, played
back on the authors. Ryan Lopopolo's
[feedback doctrine](https://github.com/lopopolo/harness-engineering/tree/trunk/docs/feedback)
(CC BY 4.0) names the cure: convert recurring corrections into the
environment — a check, a gate, a validator — because a lesson that
lives only in prose will be relearned at market price.

## The ledger

Per the series' rules, numbers come from run rows, never estimates,
and laptop-scale is labeled: these are 30-scenario runs.

| Run | Cost |
|---|---|
| Baseline | $4.14 |
| Iteration 1 (path orientation) | $4.11 |
| Iteration 2 (cache breakpoint) | $3.46 |
| Iteration 3 (truncated 12/30, API outage) | $1.60 |
| Iteration 4 | ~$3.40 |
| Iteration 5 (truncated 17/30 by billing cap, then full) | $2.18 + $4.08 |
| Iteration 6 | $4.08 |
| Iteration 7 (the certified config) | $3.93 |
| Iteration 8 (invalidated, reverted) | $4.61 |
| k=3 certification, first attempt (truncated 19/90) | $2.88 |
| **Total through the iteration era** | **≈ $39** |

Two things the ledger shows that a summary wouldn't. First, the
harness improvement paid for itself inside its own budget:
iteration 2's caching fix cut the per-run cost 16% mid-phase.
Second, the projected all-in for the complete program — finishing
k=3 certification, three model arms, a skill ablation, every
deferred item filed as an issue with its cost attached — is
**$50–55**. A five-dimension eval program for an infrastructure
agent, with a baseline, eight pre-registered iterations, a
trial-protocol certification, and a grader audit,
costs about as much as a team lunch. The expensive input was never
the tokens. It was the discipline — pre-registration, one change
per run, per-item diffs — that made each $4 run mean something.

## What's still open, on the record

The trust argument is strongest where it ends with a registered
experiment rather than a claim. The largest open question — does
this task even need the pinned flagship worker, or does the harness
carry the capability? — is pre-registered in the log with its arms
(the pinned worker against two cheaper tiers), its hypothesis (the
mid-tier lands within two items of the flagship on pass^k at
roughly 40% less cost), and its decision rule written before the run,
including the condition under which the production recommendation
flips. The k=3 certification completes first — its partial results
already corrected one flattering number, moving control honesty
from a single-run 1.00 to 0.78 under repeated trials. Whatever
these runs return, the answer will be a measurement with a
pre-committed interpretation, which is the only kind of answer this
phase has learned to trust.

## What I'd take to the next project

- **Ask the who-grades-the-graders question before someone else
  does, and answer it in layers.** Deterministic core, checks that
  don't trust their author, a record of dissent, named residual
  trust. If any layer is missing, that is the work.
- **Mutation-test any grader that gates money or merges.** Thirteen
  targeted mutants, written and run in one sitting, found two real
  gaps in a suite that was green the whole time. Make it a CI gate,
  not an event.
- **Audit the auditor's mistakes in the same log, with costs.** The
  honest-review section cost nothing to write and is the strongest
  credibility signal in the file — an instrument whose error record
  is public is an instrument you can calibrate against.
- **Promote lessons to enforcement or plan to pay for them again.**
  Every operational loss in the ledger was a lesson that existed in
  prose form before it cost money.
- **The reframe that outlasts the phase:** keep the framework human
  and let AI generate the volume — but hold the generated layer to
  the same discipline that makes the agent trustworthy: planted
  truth, pre-registration, adversarial instruments, named
  residuals. When harnesses are regenerated as fast as the models
  they grade, the eval layer is not a feature of the product; it is
  the product's warrant, and it deserves the same adversarial
  attention as anything else an AI writes.

Next phase: the analyst gets a safe runtime — permission tiers,
budgets with enforcement, audit-at-rest, and another chaos drill,
this time with the agent inside it.
