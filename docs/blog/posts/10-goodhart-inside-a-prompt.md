---
date: 2026-08-04
categories:
  - AI agents
tags:
  - evals
  - agents
  - goodhart
  - honesty
  - prompting
  - structured-output
---

# Goodhart's law operates inside a prompt

["When a measure becomes a target, it ceases to be a good
measure."](https://en.wikipedia.org/wiki/Goodhart%27s_law)
Goodhart's law is usually told about economies and institutions —
optimize a proxy and the proxy detaches from the thing it stood
for. This post watched it execute inside a single prompt, at
one-iteration speed. Across six evaluation runs, one boolean field
in an agent's report swung to whatever the surrounding prompt
rewarded — in four different directions. A rule keyed to confidence
bought confidence inflation within a single run. A rule keyed to
evidence got rationalized through its own judgment gap. One worked
example taught "when in doubt, abstain"; adding its mirror taught
"always commit."
Meanwhile the model's *checkable* claims stayed clean the whole
time: it never invented an edge or an event in any run of the phase
(the baseline's two evidence failures were real edges in an
orientation the contract hadn't defined — the previous post's
story). Then one structural change — deriving the field by
arithmetic from three verifiable booleans instead of asking for it
— took it from
worst to perfect in a single run, with no learning in between. Only
the interface changed. This post is that arc, run by run, because it
is the most useful thing this project has measured so far.

<!-- more -->

This post is about **resgraph**, a mini referential data platform
built in public, and it continues the
[previous post](09-ground-truth-first-judge-last.md): an analyst
agent triages alerts against the platform's graph and history, and
an evaluation harness grades it against causes the scenario
generator *planted* — so every number below is a comparison against
constructed fact, pre-registered before its run. The previous post
ended with the baseline's honesty score: 0.00.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-8-analyst`](https://github.com/fespino/resgraph/tree/phase-8-analyst).
    Every run below has a full entry — hypothesis, single change,
    prediction, invalidating clause, outcome — in
    [`EVALS.md`](https://github.com/fespino/resgraph/blob/phase-8-analyst/EVALS.md).

## The field that wouldn't behave

The agent's report carries a boolean: `no_confident_candidate`. On
scenarios with no planted cause — the controls, ~20% of the set —
setting it true *is* the passing answer; the quality bar demands
≥ 0.9 because a triage tool that always accuses something spends the
on-call's trust on ghosts. The baseline scored 0/6, and the failure
shape is worth staring at: every control got an accusation, but with
correctly hedged medium/low confidence labels and zero
high-confidence claims. The hedging was honest while the flag was
wrong. The model wasn't lying about what it found; the
self-assessment field simply didn't mean what the contract wanted
it to mean.

That distinction — checkable claims versus self-assessment — is the
spine of everything that follows, and it matches what the
calibration literature reports: a model's stated confidence is not
a readout of an internal credence
([On the attribution of confidence to large language models](https://arxiv.org/abs/2407.08388)
is the version of this finding the phase ended up demonstrating
experimentally). What this phase adds is the run-by-run mechanics of
*why* prompting can't fix it, and what does.

One piece of machinery to hold while reading, because every
iteration below is an edit to a specific artifact. The agent ends
every investigation by emitting a JSON report — ranked suspects,
evidence, confidence labels, and the flag — which the harness
validates against a typed schema (a Pydantic model —
[models.py](https://github.com/fespino/resgraph/blob/phase-8-analyst/src/resgraph/analyst/models.py)).
A report that violates the schema or its rules is rejected by
[`parse_and_validate`](https://github.com/fespino/resgraph/blob/phase-8-analyst/src/resgraph/analyst/harness.py#L139-L177),
and the validator's error message goes back to the model as
feedback for a retry. The system prompt, for its part, carries an
*output contract* — the prose section that specifies the report and
its rules
([prompts.py](https://github.com/fespino/resgraph/blob/phase-8-analyst/src/resgraph/analyst/prompts.py))
— plus any worked examples, and ships the investigation playbook as
a markdown skill file
([change-forensics/SKILL.md](https://github.com/fespino/resgraph/blob/phase-8-analyst/skills/change-forensics/SKILL.md)).
So there are exactly four editable surfaces: a sentence in the
contract, a demonstration in the prompt, the schema-and-validator
code, and the playbook. The iterations below work through them in
that order.

## Rules: gamed, then rationalized

The honesty work starts at iteration 3 — iterations 1 and 2 fixed
the fabrication contract and the cache design, the previous post's
story. The first honesty hypothesis was that the flag was simply
undefined, and a rule would define it.

**Iteration 3** gave the flag its first definition. The contract
now stated: `no_confident_candidate` must be true unless at least
one suspect earns *high* confidence — so listing a handful of weak,
low-confidence candidates no longer counts as having found
something. The definition was deliberately written in terms the
model already used, its own confidence labels, which looked like a
feature. As a change it was pure prose: one paragraph added to the
output contract, no code touched. The pre-registration
predicted honesty ≥ 0.83 — and, in its invalidating clause, named
the failure before the run: "controls start emitting high-confidence
accusations — a worse, different bug." That clause fired. Honesty
came back 0.33, and the run contained **four high-confidence
accusations on controls, where every previous run had zero**. The
rule made "no high-confidence suspect" the price of abstaining, and
the model paid it in the only currency the rule accepted: it
inflated the label. That is the title's claim made concrete: the
rule turned the confidence label into a target, and the label
stopped measuring confidence — Goodhart's law, executed inside a
prompt, in one iteration.

**Iteration 4** reasoned: the flag was priced in a label the model
controls, so bind it to something it doesn't — the evidence test the
graders own. The new rule — again contract prose, layered onto
iteration 3's residue rather than replacing it, a stacking that
later earned its own protocol rule: the flag may only be false for a suspect
with a graph-verified mechanism path *and* the exact change event
whose content plausibly explains the symptom. Honesty: 0.33 again.
Two controls rationalized distractors as meeting the bar — because
"plausibly explains" *contains the judgment it was meant to create*,
and hard cases sail through the gap. The rule also did new damage:
found_top3 fell 0.92 → 0.71 as the abstention language suppressed
suspect lists on the hardest causal slices, and four reports started
citing the sequence-0 snapshot row as their "exact event" — a row
that exists in the log and claims nothing. Rules were now fighting
each other.

## The diagnostic: five signals that prompt rules are exhausted

Three registered rule attempts had moved the error around without
converging (0.00 → 0.33 → 0.33), and each was *obeyed*. That
pattern generalizes, and it is the checklist this phase now applies
before writing another rule:

1. **Letter-compliance with intent-violation.** The behavior
   satisfies the rule as written while defeating its purpose —
   the inflation run is the clean example.
2. **Error displacement, not reduction.** The failure moves to an
   adjacent surface (labels, list length, a snapshot row) instead
   of shrinking.
3. **Rules start fighting each other.** A fix for one dimension
   taxes another — iteration 4's honesty rule cost 0.21 of
   found_top3 while its own target didn't move.
4. **The rule contains the judgment it was meant to create.**
   Any clause with "plausibly," "clearly," or "genuinely" in it is
   delegating the decision back to the thing being regulated.
5. **Two pre-registered misses in the same technique class.**
   After two, the protocol escalates instead of refining — the
   third rule is where this phase would have burned money
   repeating a known failure.

The escalation target is a ladder — rules → worked examples →
output structure → harness enforcement — ordered by how much
judgment each level removes from the model's discretion. It is an
application of the principle in Ryan Lopopolo's
[feedback doctrine](https://github.com/lopopolo/harness-engineering/tree/trunk/docs/feedback)
(CC BY 4.0): move each behavior to the smallest owner that can
actually enforce it, and when steering fails repeatedly, the owner
is wrong, not the steering.

## Examples: demonstrations locate postures, not boundaries

**Iteration 5** climbed to the ladder's second step and consolidated
on the way — iteration 4's rules were removed, not stacked under
the new material. In their place: one canonical worked example in
the prompt, a quiet window with a tempting radius-intersecting
correlate, walked end-to-end to the correct conclusion (correlate
listed at low confidence, flag true). It moved the behavior further
than any rule: honesty 0.83, the best of the phase to that point,
confidence inflation nearly gone. And it produced the cleanest
side-effect in the dataset: **all four transitive scenarios — the
deep causes, hardest to find — came back flag-true with 7+ of 15
tool calls unspent.** The example demonstrated only the abstain
outcome, and the model generalized it as a *posture*, applied
hardest exactly where finding was hardest. found_top3: 0.67.

**Iteration 6** did the textbook fix: make it a contrastive pair —
the quiet window stays, and its mirror joins it, a real depth-2
cause found through the same investigation, concluded flag-false
with confidence earned by mechanism and event. Demonstrating both
outcomes should teach the boundary between them. Instead honesty
collapsed 0.83 → 0.17: five of six controls now concluded
flag-false, two with high-confidence accusations. The second
example's outcome became the new posture.

Same investigation procedure demonstrated both times; the *verdict
boundary* between the two cases is what never transferred.
Anthropic's
[effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
guidance recommends curated canonical examples over rule
accumulation, and the rule half of that advice held up here — the
example did outperform every rule. The refinement this phase can
add with numbers attached: for judgment-shaped behavior,
demonstrations locate postures, not boundaries.

Six runs in, the trade-off had a shape. Honesty and found_top3, per
run: 0.00/0.92 → 0.17/0.83 → 0.33/0.71 → 0.83/0.67 → 0.17/0.71
(iteration 3's truncated run graded controls only, so it carries no
found number and sits outside the list).
Three rules, one example, one pair — every intervention moved the
operating point along a seesaw without ever holding both ends. The
judgment "does this window explain this symptom?" fit in
demonstration space no better than it fit in rule space.

## Structure: nothing left to persuade

**Iteration 7** stopped asking for the flag. Every suspect now
carries an **evidence verdict** of three booleans:

- `mechanism_verified` — does the cited dependency path hold in the
  graph at incident time? *Referentially enforced:* the harness
  validates cited edges against tool results the model actually
  received.
- `event_found` — did the cited change event appear in this run's
  tool results? *Referentially enforced* — and sequence 0, the
  initial snapshot row, never counts as an event, which closes
  iteration 4's anchoring hole by construction.
- `explains_symptom` — does the change's content account for the
  alert? The one genuine judgment bit, now isolated and named.

The whole change fits on a screen. The verdict, from
[models.py](https://github.com/fespino/resgraph/blob/phase-8-analyst/src/resgraph/analyst/models.py#L14-L23):

```python
class EvidenceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mechanism_verified: bool
    event_found: bool
    explains_symptom: bool

    @property
    def confident(self) -> bool:
        return self.mechanism_verified and self.event_found and self.explains_symptom
```

And the flag is no longer chosen: `no_confident_candidate` is false
exactly when some suspect has all three booleans true — arithmetic.
Unlike everything before it, this iteration is a *code* change, on
two of the four surfaces at once. The report schema gains the
per-suspect verdict object, and the harness's validation step — the
same machinery that already bounced malformed reports — now
recomputes the flag from the verdicts, rejects any report where
they disagree, checks `event_found` against the sequence numbers
that actually appeared in this run's tool results, and sends the
specific violation back as the retry message. The enforcement, from
[harness.py](https://github.com/fespino/resgraph/blob/phase-8-analyst/src/resgraph/analyst/harness.py#L159-L177):

```python
for i, s in enumerate(report.suspects):
    if s.verdict.event_found and s.sequence == 0:
        errors.append(
            f"suspect {i}: event_found=true but sequence 0 is the initial "
            "snapshot, not a change event"
        )
    elif s.verdict.event_found and s.sequence not in seen_sequences:
        errors.append(
            f"suspect {i}: event_found=true but sequence {s.sequence} never "
            "appeared in this run's tool results"
        )
confident = any(s.verdict.confident for s in report.suspects)
if report.no_confident_candidate == confident:
    errors.append(
        "no_confident_candidate must be exactly (no suspect has all three "
        f"verdicts true); emitted {report.no_confident_candidate} while a "
        f"fully-confident suspect present={confident}"
    )
```

The error strings are not diagnostics for a human log — they are
the retry prompt, written to steer the model's next attempt. The contract prose
shrank rather than grew: the rules the flag no longer needs came
out, and the worked examples stayed but now demonstrate the
structure, not the vibe. One more thing deliberately did not
change: the graders. The instrument held still, so runs six and
seven differ by exactly one thing.

The run came back: **honesty 1.00.** Six of six controls, zero
high-confidence accusations, the bar met for the first time — by
the first configuration with nothing to persuade. pass^k — the
overall rate at which a scenario passes *every* trial, the
harshest aggregate the harness reports — hit its
phase best (0.77), fabrications stayed at zero for the seventh
consecutive run, and the invalidating clause (controls
rationalizing `explains_symptom` on distractors) did not fire. No
prompt wisdom was added between runs six and seven. Only the
interface changed. Which means the structural fix did not make the
model more honest — the capability was there all along; the harness
had been asking for it in a gameable dialect.

This is the structure-beats-steering result that keeps appearing in
the harness literature.
[AutoHarness](https://arxiv.org/abs/2603.03329) reports synthesized
harness structure lifting a weaker model over a stronger bare one
across 145 games, and
[Agentic Harness Engineering](https://arxiv.org/abs/2604.25850) —
which evolves harnesses autonomously and ablates the result — puts
it more precisely than we could: its gains live in tools,
middleware, and memory rather than the system prompt, because
"factual harness structure transfers while prose-level strategy
does not." Here the same result reproduces at the smallest possible
scale: one field, one schema change, worst-to-perfect in one run.
And it lands on the axis the field's formalization paper,
[What makes a harness a harness](https://arxiv.org/abs/2606.10106),
calls the least formally consolidated: *control* — behavior
guaranteed by the system around the model rather than by the
model's obedience. That is exactly what the arithmetic flag is: the
honesty property moved out of the model and into the schema, where
optimization pressure has no surface to push on.

## The floor: one judgment bit has to live somewhere

Iteration 7's remaining misses were honest — every failed transitive
item concluded flag-true, hedged, with 7+ tool calls unspent. The
playbook said "widen the window if the diff is empty" but never
"deepen the radius if the intersection is empty," and the model
stopped where its playbook stopped. **Iteration 8** added that one
step — an edit to the fourth surface, the markdown playbook file:
two sentences of instruction (deepen the radius once before
concluding a window quiet, plus the matching anti-pattern), no
schema, no code. Prediction: recall recovers, honesty holds — the arithmetic
is structurally immune to an instruction about digging deeper.

Both halves missed, and the miss is the finding. Transitive
recovered only 1 of 4. And honesty dipped 1.00 → 0.67: the two new
control failures were **all-three-true verdicts on distractors that
the extra digging surfaced** — the exploration pressure leaked
through `explains_symptom`, the one bit the harness cannot check
against a store. The two enforced bits held under the same
pressure; fabrications stayed at zero for the eighth consecutive
run. Structure contains judgment to exactly the bits you can
verify. At least one unverifiable bit remains, and pressure finds
it.

The change was reverted before anything shipped, on the protocol's
own terms: one transitive item gained, two controls lost, and the
quality bar
ranks control honesty above transitive recall. The certified
configuration is iteration 7's: honesty 1.00, found_top3 0.71,
pass^k 0.77, fabrications zero on every run ever executed — a
harness that is *honest-but-shallow*, every remaining miss
truthfully reported as a miss.

One epilogue number, because single runs flatter: when the k=3
trial protocol started re-running each scenario three times and
passing only unanimous results, control honesty measured **0.78** —
five controls at 3/3, one at 2/3. Iteration 7's 6/6 was partly the
lucky edge of its distribution. That is the number the
certification carries, and the reason the protocol exists.

## What I'd take to the next project

- **Split self-assessment from checkable claims before grading
  either.** This model never fabricated an edge in eight runs while
  its self-report swung with every prompt breeze. Grade them
  together and both readings are noise.
- **A rule that prices a behavior in a label the model controls
  will be paid in that label.** Write the adversarial pre-mortem
  sentence — "how could this be satisfied by letter without the
  intended behavior?" — before the run, not after.
- **Escalate on the second miss in a technique class.** The five
  signals above are cheap to check and each one, in this log, was
  visible a full paid run before it was acted on.
- **Derive judgments from verifiable atoms wherever the atoms
  exist.** The flag was unfixable as a request and trivial as
  arithmetic. Most "the model is being dishonest" complaints are
  interface bugs of this shape.
- **Name the residual bit.** After structure does its work, the
  judgment that remains (`explains_symptom` here) is small, known,
  and measurable — which is the best available state, because
  pressure *will* find it.

The remaining question an attentive reader should be asking: the
graders held up while the prompt was being gamed — but the graders
were written by an AI too, inside the same project. Why trust the
instrument? That question got its own audit, with a methodology
designed to fail loudly, a running cost ledger, and a list of the
author's own mistakes. It is the next and final post of this trio.
