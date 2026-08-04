---
date: 2026-08-04
categories:
  - AI agents
tags:
  - evals
  - agents
  - llm-judge
  - synthetic-data
  - pre-registration
  - prompt-caching
---

# Ground truth first, judge last

The first graded run of resgraph's new incident-triage agent looked
like a success: the planted root cause appeared in the top-3
suspects 92% of the time. The same run scored 0.00 on honesty —
all six no-cause controls got an accusation anyway — and halted the
whole iteration loop on two mechanism paths that failed verification
against the graph. One iteration later, fixing the verification
failures made the 92% *drop* to 83% — in part because one of those
hits had been subsidized by an invalid path all along. None of this
came from watching demos and feeling uneasy; every number came off
graders that compare the agent's claims against a cause the test
planted. This post is about building that instrument — before
trusting anything the agent appears to do.

<!-- more -->

This post is about **resgraph**, a mini referential data platform
built in public. Part I built the pipeline: a deterministic
generator streams infrastructure updates, a consumer applies them
idempotently into a graph store, a cold store keeps full history in
Iceberg, one query layer answers questions needing both, and an
observability layer watches it all. Part II gives the platform its
intended consumer: an agent. This phase adds an analyst that triages
an alert the way an on-call engineer would — walk the dependencies,
diff the window, read the change history, name ranked suspects with
evidence (D22 in the spec: one agent, a 15-call tool budget, five
read-only tools derived from the platform's tool registry, a
structured report contract). But the agent is the second half of the
phase. The first half, and this post, is the evaluation harness that
grades it (D24) and the scenarios it is graded on (D25) — built and
committed before the first graded run.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-8-analyst`](https://github.com/fespino/resgraph/tree/phase-8-analyst).
    The full run-by-run log this post draws on is
    [`EVALS.md`](https://github.com/fespino/resgraph/blob/phase-8-analyst/EVALS.md).

## The bar was written before the harness

The first committed artifact of the phase is a problem-discovery
memo — what human triage actually involves, what it costs, and what
"good" means, defined measurably before the first run:

- **Found:** planted cause in the top-3 suspects ≥ 80% of scenarios;
  top-1 ≥ 60%.
- **Fabricated evidence = 0.** Not low — zero. Every mechanism edge
  a suspect cites must have existed in the graph at incident time;
  every cited change must exist in the event log. A triage tool that
  invents an edge is worse than no tool, and any run where one
  appears halts iteration until it is zero again.
- **Honesty on controls ≥ 90%:** on scenarios with no planted cause,
  "no confident candidate" is the *passing* answer. A
  high-confidence wrong answer scores worse than an honest miss —
  what the epistemic-trust literature calls *virtuous abstention*
  ([arXiv:2603.02960](https://arxiv.org/abs/2603.02960)): declining
  with reasons graded as competence, not failure.
- **Budgets:** wall-clock, cost, and cache behavior inside committed
  limits.

The ordering is the point. Written after the first run, a bar bends
toward whatever the agent happens to do; written before, the file's
git history is the witness that it never bends quietly. When one
target later turned out to be genuinely wrong (more below), changing
it took a dated amendment in the file, in the spec's correction
table, and in the eval log — expensive on purpose.

Two sources shaped this section of the build more than any code.
Hamel Husain's
[Your AI Product Needs Evals](https://hamel.dev/blog/posts/evals/)
puts unit-test-like checks first, human/model review second, A/B
last, and insists that "you can never stop looking at data" — the
per-item transcript reading that every finding below came from.
Anthropic's
[Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
recommends starting with a small task set drawn from real failure
modes and reading transcripts as a non-negotiable step, and supplies
the metric vocabulary the harness uses (`pass@k`: at least one of k
trials passes — capability; `pass^k`: all k trials pass —
reliability, the number an on-call actually experiences).

## Ground truth is constructed, not judged

The eval's central trick is inherited from Part I: the platform
already has a deterministic synthetic cloud, so the test can *plant*
the truth it grades against. A scenario generator extends the world
generator: it picks a victim resource, plants a causal change (a
config change on a direct dependency, a deleted resource, a cause
two or three hops up the dependency chain), lets unrelated churn
continue around it, then fires an alert downstream. The scenario
spec records the planted cause, the mechanism path, and the exact
event sequence number — so grading "did the agent find it?" is a
string comparison, not an opinion.

The taxonomy has seven shapes, and the distribution is an argument,
not an accident:

- **direct**, **deleted-resource**, **transitive (depth 2–3)** — the
  causal spectrum, easy to hard;
- **noisy-window** — the cause plus ten-plus distractor changes in
  the same window;
- **ambiguous** — two plausible candidates, one real;
- **decoy** — a seductive non-causal change planted *beside* the
  real cause, engineered to be the last change before the alert;
- **control** — no cause at all, ~20% of the set, because an agent
  never shown "nothing" learns to always accuse something.

That last sentence was written into the spec as a prediction, and
the baseline run measured it at its maximum: honesty 0/6. More on
that below.

Two design choices are defenses against eval decay. Each scenario
can be *re-skinned* — same causal structure, fresh resource names
and identifiers — so the set can be regenerated if it ever leaks
into training data or gets overfit by prompt iteration; Anthropic's
[AI-resistant technical evaluations](https://www.anthropic.com/engineering/AI-resistant-technical-evaluations)
measures how fast evals erode as models improve (an internal
take-home that took 2,164 cycles to solve dropped to 1,487 across a
single model generation) and reaches for novelty as the recovery —
re-skins
are novelty by construction. And the scenarios are generated by
code, not by an LLM: Microsoft's
[STATE-Bench](https://github.com/microsoft/STATE-Bench) discloses
LLM-synthesized tasks as its scaling trade-off, which is the honest
version of a real alternative — but a synthesized task's ground
truth is only as reliable as the model that wrote it, and this
harness exists precisely to avoid grading judgment with judgment.

## Graders first, judge last

Five dimensions, and the design rule is in the title: four of them
are deterministic code, and the LLM judge comes last, weighted
smallest, grading the only thing code can't — whether the triage
narrative is coherent and evidence-grounded prose.

- **found** — is the planted cause in top-1 / top-3? String
  comparison against the scenario spec.
- **evidence** — does every cited mechanism edge exist in the graph
  at incident time, every cited event in the log? Verified with the
  same as-of query the production API uses. This grader is the halt
  condition.
- **honesty** — on controls, did the agent conclude "no confident
  candidate"?
- **discipline** — budget adherence: tool calls, parse retries,
  cache behavior.
- **narrative** — the judge, pinned to a model and a frozen prompt
  template.

The judge pin taught its own lesson: the spec originally pinned it
at temperature 0 with a fixed seed, and the API rejected the first
and never offered the second. A contract isn't a pin until a real
call has passed through it — the correction is a dated row in the
spec, and the working pin is model + template only, which is one
reason the judge gets the smallest weight: it is the least pinnable
instrument in the stack.

Scoring uses the pass^k trial protocol — k=3 runs per scenario, pass
only if all three pass — but not from day one. Trials on a harness
that is still changing every run would measure noise at triple
price, so trials start after the fabrication halt clears and the
big failure buckets are fixed. STATE-Bench's headline pairing of
pass@1 with pass^5 is the same reasoning at benchmark scale:
capability and reliability are different numbers, and reliability
is the one that costs money to know.

## The protocol that makes a run mean something

The measurement discipline is adapted from Ryan Lopopolo's
[harness-engineering evals doctrine](https://github.com/lopopolo/harness-engineering/tree/trunk/evals/README.md)
(CC BY 4.0), whose core is pre-registration: before a run, write
down the hypothesis, the single change, the predicted effect, and —
the part that does the most work — the result that would *invalidate*
the hypothesis. His
[effectiveness notes](https://github.com/lopopolo/harness-engineering/tree/trunk/docs/effectiveness)
compress the why into one line: "repeating a known failure without
new evidence is waste."

That doctrine was read twice, and the second reading was a
different document. The first pass, before any eval ran,
transferred the frameworks — pre-registration, the ledger shape. A
re-read after four iterations found sentences we had walked past
that turned out to be our run log written in advance: "a signal is
a lead rather than a diagnosis," whose misdiagnosis classes are
exactly the four things that happened to this project inside 24
hours, and the consolidation-over-accumulation rule whose failure
mode — competing instructions — one of our runs had just measured
as a 0.21 recall bleed. Both became protocol rules the same day.
The general lesson: pre-build reading transfers frameworks;
post-experiment re-reading transfers judgment, because operational
sentences only bind once you have paid for their absence.

The full rule set, as it stood by the end of the phase (each rule
has a run that earned it, recorded in `EVALS.md`):

- **One change per iteration.** Two changes per run means no
  attribution; every "obvious" bundling temptation in this phase
  turned out to hide the interesting result.
- **Per-item diffs over score deltas.** The fabrication-subsidy
  finding below is invisible at the score level and one line in an
  item diff.
- **Unregistered movement is variance until proven otherwise.**
  At 30 scenarios and one trial, a slice is 4–6 items and ±1 item
  swings it by 0.17–0.25. When a metric nobody targeted moves, the
  protocol forbids claiming credit. Anthropic's
  [measurement of infrastructure noise](https://www.anthropic.com/engineering/infrastructure-noise)
  makes the same point at scale: identical configurations can swing
  several points on agentic benchmarks from the environment alone.
- **Signal triage precedes hypothesis.** A failing dimension is a
  lead, not a diagnosis — first classify it: harness gap, worker
  variance, external failure, or a mis-built metric. This phase
  produced one of each.
- **An adversarial pre-mortem in every registration:** one required
  sentence — "how could the model satisfy this change's letter
  without the intended behavior?" (Added mid-phase, after a run
  that the next post covers in detail.)

## What the baseline paid back

The baseline ran unpolished on purpose — the memo's bar, the
contract as first drafted, no tuning:

```
pass^k 0.67   found_top1 0.71   found_top3 0.92   evidence 0.92
honesty 0.00  discipline 0.00   narrative 1.00    cache 0.68
fabrications 2 → HALT
```

Three findings, one per failure class, all from run one and the two
registered iterations that followed.

**The predicted failure arrived on schedule.** All six controls got
an accusation — with correctly hedged medium/low confidence labels,
which makes it more interesting, not less: the model hedged honestly
while the *conclusion field* was wrong. The taxonomy's ~20% control
share existed because of exactly this failure mode, and the baseline
measured the design sentence at its maximum. What it took to
actually fix it is the next post; it is the best story in the phase.

**A fabricated path was subsidizing the accuracy score.** The two
evidence failures were real edges cited in an orientation the output
contract had never defined — the grader was right to halt on them: a
path claiming "the VM depends on the container" misleads the
on-call even when both nodes are real. Iteration 1 defined path
orientation, cleared the halt (evidence 0.92 → 1.00) — and
found_top3 *fell* 0.92 → 0.83 — two items, both decoy. The item
diff explains them: one scenario had the planted cause in its top-3
alongside an invalid mechanism path — ban invention, and the agent
ranks its honest reading first and the "hit" evaporates, because it
was never earned; the other shortened its suspect list under the
stricter contract (one suspect where the baseline listed three) and
the plant went with it. A score-only dashboard would have called
this iteration a regression. It was the eval working: when honesty
goes up and accuracy goes down, the accuracy was borrowing against
the honesty.

**The metric that couldn't be reached was the design review that
worked.** Discipline scored 0/30, entirely on one check: cache hit
rate ≥ 0.9. The token columns showed why, and the wrong-looking
number was the diagnostic: `cache_creation` was zero and
`cache_read` was always an exact multiple of 4,126 — the prompt
prefix, read once per turn, while the growing conversation re-billed
as plain input on every turn, 250,754 uncached tokens across the
run. The harness had placed exactly one
[cache breakpoint](https://docs.claude.com/en/docs/build-with-claude/prompt-caching),
at the prefix end, so the one thing that grows at runtime was the
one thing never cached — and token-weighted cache hit *falls* as
runs lengthen, making 0.9 structurally unreachable. The phase's
prompt-audit table had a verdict for every prompt section and still
missed this: the transcript, the only section that grows at
runtime, wasn't a row. Iteration 2
moved a second breakpoint onto the last message block: uncached
input 250,754 → 234 tokens, cost $4.14 → $3.46 per run. The eval
paid for its own improvement inside two iterations.

And then the floor still failed — 0 of 30 rows reached 0.9, because
everything left was one-time cache *writes*, the cost every new
token owes before it can ever be read. That is a metric-definition
bug, not a runtime bug: the gate was penalizing cost, and a gate may
only penalize what the harness can avoid. The fix is a dated
amendment — discipline now gates on the uncached *re-read* fraction
(≤ 0.1), the waste the metric was built to catch, with cache hit
still reported. The unreachable target did its job by being
unreachable: a metric you can't game surfaces the design error the
design review missed.

## What I'd take to the next project

- **Write the bar before the harness, and make it expensive to
  change.** The one target that was genuinely wrong cost a dated
  amendment in three files. That friction is the feature — it is
  the difference between calibrating an instrument and bending one.
- **Plant the truth; don't ask a model to recognize it.** Every
  finding above is a comparison against a cause the generator
  planted. The judge grades prose, last and smallest, because it is
  the least pinnable instrument in the stack.
- **Pre-register or you will fool yourself politely.** The
  invalidating-result clause, written before the run, is what
  separates "the fix worked" from "the numbers moved."
- **Read item diffs, not dashboards.** The fabrication subsidy and
  the cache diagnosis were both invisible at score level and obvious
  one level down.
- **When a target is unreachable, suspect the design before the
  worker.** The best outcome this phase's discipline metric produced
  was a metric amendment and a caching fix — and it produced both by
  refusing to pass.

The baseline also left honesty at 0.00, and the memo demands 0.90.
Closing that gap took six more pre-registered iterations, four of
which failed in instructive ways — including one where the model
satisfied a new rule's letter within a single run by inflating
exactly the label the rule had priced. That arc — when prompt rules
stop working, how to tell, and what finally fixed it — is the next
post.
