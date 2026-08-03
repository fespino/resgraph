# EVALS — the analyst's iteration log

The quality bar lives in `docs/discovery/incident-triage.md`, written
before any harness code. This file records every run against it:
baseline first and unpolished, then one fix per iteration, each
pre-registered (hypothesis → single change → predicted effect →
invalidating result) before its run. The committed baseline is
`evals/baseline.json`; it refreshes only via labeled commits.

Two protocol rules added 2026-08-03, after iterations 1–4:

- **Signal triage precedes hypothesis.** A failing dimension is a
  lead, not a diagnosis: before registering a fix, classify the
  signal — harness gap, worker variance, external failure, or overfit
  control. This file already contains one of each (the flag
  semantics, the trials=1 slice swings, the truncated runs, the 0.9
  cache floor); iterations 3–4 skipped the step and assumed
  harness gap.
- **Prompt changes consolidate, never stack.** A superseded rule is
  removed when its replacement lands — accumulated rule sediment
  becomes competing instructions (iteration 4's evidence-bar rule
  cost the causal slices 0.21 of found_top3 while its target bucket
  didn't move).

Environment pin (all runs unless a row says otherwise): model
`claude-opus-4-8`, adaptive thinking, judge = same model + pinned
template, 30-scenario dataset `evals/scenarios/base.jsonl` (seed 42),
trials 1 during early iteration (the k=3 trial protocol starts once
the fabrication halt clears and the big buckets are fixed — pass^k on
a moving harness would measure noise).

## The iteration arc — why each change happened

Each iteration's entry below carries its full pre-registration and
outcome; this is the connective tissue — what each run taught that
made the next one necessary:

1. **Iteration 1 (path orientation)** — because the baseline's two
   "fabrications" were real edges cited in an orientation the
   contract never defined. Why first: fabrication is the halt
   condition; nothing else iterates until it is zero.
2. **Iteration 2 (transcript cache breakpoint)** — because the
   baseline's token columns showed only the prefix was cached and
   the growing conversation re-billed every turn; the 0.9 target was
   structurally unreachable. Why the metric amendment followed: with
   re-billing at zero the floor still failed, because it penalized
   one-time cache writes — cost, not waste. A gate may only penalize
   what the harness can avoid.
3. **Iteration 3 (flag defined by confidence)** — because every
   control accused a suspect while hedging correctly; the flag's
   semantics were simply undefined. Why it failed: it priced the
   flag in a label the model controls, and the model inflated the
   label (Goodhart, one iteration).
4. **Iteration 4 (flag bound to evidence)** — because iteration 3
   showed labels are purchasable; evidence criteria seemed not to
   be. Why it failed: "plausibly explains" contains the judgment it
   was meant to create, hard cases rationalized through the gap, and
   the added rule suppressed recall on causal slices — rules were
   now fighting each other.
5. **Iteration 5 (worked example replaces the rules)** — because
   three rule attempts moved error without converging:
   letter-compliance each time, intended behavior never. Rules
   describe a boundary; examples locate it. Why it half-worked:
   honesty hit its phase best, but a single demonstrated outcome
   taught "when in doubt, abstain" as a posture — transitive causes
   started reading as quiet windows.
6. **Iteration 6 (contrastive pair)** — because one demonstrated
   outcome teaches a bias; two teach the boundary between them. The
   quiet window stays, its mirror joins it: same investigation, a
   real depth-2 cause, the opposite verdict — flag false, confidence
   earned by mechanism and event.

## Baseline — run `20260803T031719Z` (git `dae5f82`, $4.14)

```
pass^k 0.67   found_top1 0.71   found_top3 0.92   evidence 0.92
honesty 0.00  discipline 0.00   narrative 1.00    cache 0.68
latency p50 43.7s p95 88.6s     fabrications 2 → HALT
```

Per-slice: direct 1.00, deleted_resource 1.00, noisy_window 1.00,
ambiguous 1.00, transitive 0.50, decoy 0.50, control 0.00.

What run 1 actually says, bucketed per the failure taxonomy:

- **Fabrication halt (2, both decoy slice) — attribution: harness
  bug, prompt side.** Neither is an invented node or event; both are
  mechanism paths written in the wrong orientation on
  self-cause/reschedule shapes (the cited edge exists — reversed; the
  cited sequences are real log events). The grader is right to fail
  them: a path claiming "the VM depends on the container" misleads
  the on-call. The output contract never defined path orientation or
  the single-node self-cause path. Iteration stops until this is
  zero.
- **Honesty 0/6 — attribution: model behavior, predicted by design.**
  Every control run accused a suspect instead of concluding "no
  confident candidate." D25 put controls at ~20% of the set because
  "an agent never shown 'nothing' learns to always accuse something";
  run 1 measured that sentence at its maximum.
- **Discipline 0/30, entirely the cache floor — attribution: harness
  design gap.** Every failure detail is `cache hit < 0.9`; there are
  no repeated identical calls and no parse retries anywhere in the
  run (structured output parsed first try, 30/30). The token columns
  explain the floor: `cache_creation = 0`, `cache_read` always a
  multiple of 4,126 — only the prefix is cached, and the growing
  transcript re-bills as plain input every turn, because the prompt
  carries exactly one breakpoint. Token-weighted cache hit therefore
  *falls* as runs lengthen; 0.9 is structurally unreachable with a
  prefix-only breakpoint. The committed target did its job by being
  unreachable.
- **Transitive 0.50 — depth splits it.** Both depth-2 items pass with
  top-1 found; both depth-3 items miss the planted cause entirely.
  The lagging slice the taxonomy exists to expose; not this
  iteration's target.
- Above the bar already: found_top3 0.92 (bar: ≥0.80), found_top1
  0.71 (bar: ≥0.60), evidence 0.92 on non-fabricated paths, first-try
  parsing 30/30.

## Iteration 1 — pre-registered 2026-08-03, run `20260803T121152Z` ($4.11)

**Outcome: halt cleared.** Fabrications 2 → 0, evidence 0.92 → 1.00
— the hypothesis held: the model was citing real edges in undefined
orientations, and defining the orientation fixed it (decoy-s42007 now
reports the self-cause as a single-element path, exactly the new
rule). **One prediction missed, instructively:** decoy stayed at 0.50
and found_top3 dipped 0.92 → 0.83 (two items, both decoy). Item diff
shows why: in the baseline, a fabricated path was *subsidizing* the
found score — s42007 had the planted cause in top-3 alongside an
invalid mechanism; with invention banned it ranks its honest
self-cause reading first and drops the plant. The stricter contract
also shortens suspect lists (s42019: one suspect where run 1 gave
three). Both are the trade the memo endorses — an honest miss
outranks an invalid hit — and the shorter-list effect is noted as a
candidate cause when the honesty iteration touches abstention rules.
pass^k unchanged at 0.67; every other slice identical run-over-run.

### As pre-registered:

- **Hypothesis:** the two fabrications are contract ambiguity, not
  invention — the model cites real edges in undefined orientations.
- **Change (one):** the output contract defines path orientation
  (each consecutive pair (a, b): b must cite a in its relationships
  at incident time) and the self-cause case (a single-element path;
  never pad with neighbors).
- **Predicted:** fabrications 2 → 0; decoy slice 0.50 → ≥0.75; other
  dims unmoved. The prefix changes, so the cache fingerprint changes
  — expected and labeled.
- **Invalidating result:** fabrications persist with correct
  orientation, meaning the model is inventing edges after all —
  attribution moves to model behavior and the fix moves to the
  harness's referential checks.

## Iteration 2 — pre-registered 2026-08-03, run `20260803T124249Z` ($3.46)

**Outcome: the waste is gone; the floor is wrong.** Uncached input
across the whole 30-scenario run: 234 tokens (baseline: 250,754) —
re-read fraction 0.000; the transcript re-billing the breakpoint was
built to kill is eliminated entirely. Cost $4.14 → $3.46 per run.
Cache hit 0.68 → 0.85 mean — and 0 of 30 rows reach the 0.9 floor,
because every remaining non-read token is `cache_creation` (110,975
across the run): the one-time write each NEW token must pay before it
can ever be read. Short 3–5-turn runs never amortize writes below
10% of input. The pre-registration's invalidating clause ("below 0.9
→ the message-order tests have a hole") was too binary — the data
shows a third outcome it didn't anticipate: zero waste AND floor
missed, which is a metric-definition bug, not a runtime bug. Per the
failure taxonomy that is an **eval bug — our failure**, and the
proposed fix is a labeled amendment (discipline gates on uncached
re-read fraction ≤ 0.1, the waste the metric was built to catch;
cache-hit stays reported), pending sign-off — the memo's no-quiet-
bar-bending rule applies to the bar itself.

**Unregistered movements = variance until proven otherwise.** pass^k
0.67 → 0.73, honesty 0.00 → 0.17, decoy and transitive up, direct
down 0.25 — none of these had a registered change targeting them,
and at trials=1 each slice is 4–6 items, so ±1 item swings a slice
by 0.17–0.25. No credit claimed, no blame assigned; this is the
argument for starting the k=3 trial protocol once the metric
amendment lands.

### As pre-registered:

- **Hypothesis:** the cache floor is the prefix-only breakpoint, not
  runtime behavior (fingerprint was stable all run).
- **Change (one):** the harness marks the last message block of each
  request with `cache_control`, so the transcript caches
  incrementally turn over turn (the append-only message invariant
  makes it prefix-stable; the API allows 4 breakpoints, we use 2).
- **Predicted:** cache ≥ 0.9 on multi-turn runs; discipline 0.00 →
  ≥0.9; input cost per run drops noticeably; latency p50 improves.
- **Invalidating result:** cache stays below 0.9 with the transcript
  breakpoint in place — the miss is then runtime message mutation,
  and the message-order invariant tests have a hole to find.

## Iteration 3 — pre-registered 2026-08-03, run `20260803T142459Z` (partial: 12/30, API outage; all 6 controls completed — decisive for this hypothesis)

**Outcome: prediction missed; the second invalidating clause fired.**
Honesty 2/6 (predicted ≥ 0.83) — and the failure shape is new:
**four high-confidence accusations on controls**, where every prior
run had zero. Defining the flag in terms of confidence ("true unless
a suspect earns high") handed the model a ticket price for keeping
the flag false — and it paid it by inflating confidence. The rule
created the incentive; the model followed it. This is Goodhart's law
executed in a single iteration, caught only because the
pre-registration named the failure before the run: "controls start
emitting high-confidence accusations — a worse, different bug."

What the partial run also showed: the causal half that ran went 6/6
on found_top3 and evidence (fabrications still 0), and the amended
discipline gate passed 11/12 — iteration 2's metric amendment
behaves as intended under real traffic.

Attribution for iteration 4: the flag cannot be derived from a field
the model prices. The abstention rule must bind to the *evidence
test* the graders already own — high confidence requires a verified
mechanism plus the exact event, and a window with only
correlation-level candidates is a no-confident-candidate case
regardless of how the suspects are labeled.

### As pre-registered:

- **Hypothesis:** controls fail because `no_confident_candidate` is
  undefined relative to confidence. The run-3 shape: five of six
  controls list only medium/low suspects yet set the flag false; the
  single pass sets it true *while still listing a low-confidence
  candidate*. The model reads the flag as "did I find anything to
  list," not "do I have a confident diagnosis" — and never emits a
  high-confidence accusation on a control, so the confidence
  calibration itself is not the bug.
- **Change (one):** the output contract defines the flag:
  no_confident_candidate=true unless at least one suspect earns
  confidence high; listing weak correlation-only candidates is
  compatible with the flag being true.
- **Predicted:** honesty 0.17 → ≥ 0.83; found/evidence unmoved (the
  flag gates nothing they grade). Prefix changes; fingerprint change
  labeled.
- **Invalidating result:** controls still set the flag false over
  medium-only lists (the flag definition isn't the binding
  constraint), or controls start emitting high-confidence
  accusations (a worse, different bug in confidence semantics).

## Iteration 4 — pre-registered 2026-08-03, run `20260803T153308Z` ($3.4x, full 30)

**Outcome: prediction missed; the invalidating clause fired; and the
rule bled into causal recall.** Honesty 0.33 (predicted ≥ 0.67):
high-confidence control accusations fell 4 → 2 but two controls still
rationalize distractors as meeting the evidence bar, and two keep a
false flag over medium-top lists the rule forbids. The new damage:
**found_top3 0.92 → 0.71 against baseline** (transitive 0.25, direct
and noisy 0.75) — the abstention language suppresses recall on hard
causal slices: failing items now carry 1–2-suspect lists (baseline:
3+), several with the flag true over a list that omits the planted
cause entirely. A new failure shape appeared alongside it:
**sequence-0 anchoring** — four failing reports cite the snapshot row
(seq 0, a resource's initial state, not a change) as a suspect
"change"; it passes the log-existence check because snapshots are in
the log, but it is semantically an empty claim. Recorded as a
candidate grader question (should cited causal changes require
sequence > 0?), not silently patched.

Also fully validated on a complete run: discipline 1.00 under the
amended gate, evidence 1.00, fabrications 0 for the third
consecutive run.

**Three registered attempts at the honesty bucket now show
letter-compliance with moved error, not convergence** (0.00 →
rule → 0.33 with inflation → 0.33 with recall damage). Per
iteration 4's invalidating clause, prompt rules are treated as
exhausted for this behavior; the next registered change is a
canonical worked example, and if that fails the judgment moves into
the output structure itself (a per-suspect evidence-verdict field the
graders can check directly).

### As pre-registered:

- **Hypothesis:** iteration 3 failed because the flag was priced in a
  label the model controls. Binding it to the evidence test the
  graders own removes the exchange rate: there is nothing the model
  can inflate to buy a false flag.
- **Change (one):** the flag rule becomes evidence-bound —
  no_confident_candidate=false requires a suspect with BOTH a
  graph-verified mechanism path at incident time AND the exact change
  event whose content plausibly explains the symptom; correlation
  inside the window never clears the bar; flag and confidence are
  declared independently graded.
- **Predicted (conservative after two misses):** honesty ≥ 0.67;
  control high-confidence accusations return to 0; found/evidence
  unmoved; fabrications 0.
- **Invalidating result:** controls rationalize distractors as
  meeting the evidence bar — prompt rules have hit their limit, and
  the next move is a canonical worked example in the prefix, not
  more rules.

## Iteration 5 — pre-registered 2026-08-03, run `20260803T162225Z` (partial: 17/30, org spend cap; all 6 controls completed — $2.18)

**Outcome: best honesty yet, one short of the prediction; causal
readings truncated below attributability.** Honesty 3/6 (predicted
≥ 4/6, invalidating < 3/6 — the middle band): the worked example
moved the behavior further than any rule (0 → 1 → 2 → 2 → 3 across
the series) and confidence inflation is nearly gone (high-confidence
control accusations 4 → 1). The causal half ran 11 of 24 items —
decoy 0/2 and transitive 0/2 on the truncated sample are too small
to separate example-harm from variance, and are recorded as
unattributed. Fabrications 0 (fourth consecutive run); discipline
1.00; evidence 1.00.

The truncation cause is a new external-failure class for the signal
triage list: the org's monthly API spend cap — the billing layer's
own T4, a control independent of anyone's obedience, including ours.

**Standing decision at the cap:** finish the phase's remaining runs
(≈ $30: honesty resolution, k=3 baseline, model arms, skill arm)
requires raising the Console limit; otherwise the phase concludes by
characterizing the honesty residual on the recorded five iterations
— which the memo permits — and the pre-registered experiments carry
to next month unchanged.

**Full re-run `20260803T170527Z` ($4.08, 30/30): the honesty
component CONFIRMED, the recall component measured as one-sided
demonstration bias.** Honesty 0.83 (5/6 — prediction ≥ 0.67 met;
best of the phase; one high-confidence accusation remains). pass^k
0.70, best yet. Fabrications 0, fifth consecutive run. But
found_top3 fell to 0.67 with **transitive 0/4 — and every transitive
failure sets the flag TRUE**: the model now treats deep-cause
windows as quiet windows, several anchoring on the sequence-0
snapshot again. The single worked example demonstrated only the
abstain outcome, and the model over-applies it exactly where finding
is hardest. Textbook one-sided few-shot bias; the honesty/recall
tension is now the phase's measured central finding: baseline was
honesty 0.00 / found_top3 0.92; v5 is honesty 0.83 / found_top3
0.67. Two latency outliers (~650s, API stalls) inflate p95; noted,
not investigated.

## Iteration 6 — pre-registered 2026-08-03, run `20260803T185628Z` ($4.08, 30/30)

**Outcome: invalidated, first branch — the pair taught "always
commit."** Honesty collapsed 5/6 → 1/6: five controls now conclude
flag-false, mostly over medium-top lists, two with high-confidence
accusations. And the predicted recall recovery barely arrived:
found_top3 0.71 (predicted ≥ 0.79), transitive 1/4 (predicted
≥ 2/4). Fabrications 0 for the sixth consecutive run; evidence 1.00;
discipline 29/30.

The six-run seesaw is now the phase's cleanest dataset — honesty /
found_top3 per run: baseline 0.00/0.92 → it1 0.17/0.83 → it4
0.33/0.71 → it5 0.83/0.67 → it6 0.17/0.71. Every prompt-level
intervention — three rule attempts, one example, one contrastive
pair — moved the trade-off point without ever holding both sides.
The judgment "does this window explain this symptom?" does not fit
in demonstration space any better than it fit in rule space.

**Escalation, as pre-registered twice: ladder step 3.** The judgment
moves into the output structure — per-suspect evidence-verdict
fields (mechanism verified at incident time? exact event found? does
it explain the symptom?) that the model must fill, the harness
validates deterministically with descriptive feedback (flag false
requires a suspect with all verdicts true), and the graders check
directly against the store. Nothing left to persuade; the flag
becomes derived, not chosen. Awaiting go — it is a schema + harness
+ grader change, the phase's first structural iteration.

### As pre-registered (iteration 6):

- **Signal triage:** harness gap, demonstration-shaped — the same
  class as iteration 5, now on the opposite side: consistent
  over-abstention on transitive items (4/4 flag-true misses), not
  variance.
- **Change (one, still ladder step 2):** the worked example becomes a
  **contrastive pair** — the existing quiet-window case plus a
  deep-cause case: a transitive (depth-2) fault found through the
  radius walk, concluded with the flag FALSE and confidence earned
  by the mechanism and the exact event. Demonstrating both outcomes
  teaches the discrimination; demonstrating one taught a bias.
- **Predicted:** transitive ≥ 0.50, found_top3 ≥ 0.79, honesty holds
  ≥ 0.67, fabrications 0.
- **Invalidating result:** recall recovers but honesty collapses (the
  pair teaches "always commit") or both stay flat — examples
  exhausted, judgment moves to the evidence-verdict output fields.

### As pre-registered (iteration 5):

- **Signal triage (protocol rule 1):** harness gap, demonstration-
  shaped — not worker variance (controls failed consistently across
  four runs: 0/6, 1/6, 2/6, 2/6), not external, not an overfit
  control (the honesty grader was never gamed; the prompt was).
  Three rule attempts moved error without converging: the prompt
  lacks a demonstration of the joint judgment, not another clause.
- **Change (one, ladder step 2, consolidating per protocol rule 2):**
  iteration 4's evidence-bar rules are REMOVED, replaced by one
  coarse flag rule plus a canonical worked example in the prefix — a
  quiet window with a tempting radius-intersecting correlate, shown
  end-to-end to the correct conclusion: correlate listed at low
  confidence with an evidence line saying why it falls short, flag
  true. Enabling fix folded in: citable ids now seed only from
  post-breakpoint blocks and tool results — the prefix was already
  leaking the skill body's example ids (vm-000047, container-000123)
  into the citable set, so example ids could have passed referential
  validation and surfaced as grader-level fabrications.
- **Predicted:** honesty ≥ 0.67; control high-confidence accusations
  0; found_top3 recovers toward ≥ 0.83 as the competing instruction
  is removed; fabrications 0. Prefix grows; fingerprint change
  labeled.
- **Invalidating result:** honesty < 0.5 — demonstration is
  insufficient and the judgment moves into the output structure
  (per-suspect evidence-verdict fields, harness-validated with
  descriptive feedback).

## Grader verification — mutation testing (2026-08-03)

**Goal.** The graders decide every number in this file, and the same
author wrote the graders and the system under test. "The tests pass"
does not establish that the tests *constrain* the graders — a test
suite can accompany code without gripping it. Mutation testing
measures the grip directly: break each grader on purpose and require
the suite to notice.

**Methodology.** Thirteen targeted semantic mutants — not random
token flips but the specific bugs each grader exists to prevent:
comparisons inverted (found_top1), the top-3 window removed, the
evidence edge-orientation flipped (the exact bug class iteration 1
was about), edge checking disabled outright, the log-existence check
inverted, the honesty conjunction weakened AND→OR, high-confidence
detection retargeted, the repeated-call and parse-first-try and
uncached-fraction checks disabled or inverted, pass^k silently
computed as pass@k, controls made to always pass, and the judge's
pass boundary moved by one. Each mutant is applied alone, the grader
suite runs against it, and the original is restored regardless of
outcome. KILLED means the suite failed (the tests noticed); SURVIVED
means the suite stayed green while a grader lied — a test gap. The
driver is committed at `evals/meta/mutate_graders.py`, exits nonzero
on survivors, and is re-run after any grader change; a survivor must
become a test before the change merges.

**Results.** First pass: 11/13 killed. Both survivors were genuine
gaps, not artifacts: no test pinned the inconsistent state of a true
flag alongside a high-confidence suspect (so weakening the honesty
conjunction passed), and no test exercised the judge's boundary
score of exactly 3 (so an off-by-one passed). Both became tests in
the same commit; re-run: 13/13 killed. The audit finding holes is
what distinguishes it from a ritual.

## Pre-registered experiment — model arms (runs after the harness stabilizes)

Question on the record (Fran, 2026-08-03): does task complexity
justify the pinned Opus worker? Answered by measurement, not
judgment:

- **Arms:** `claude-opus-4-8` (the pinned worker), `claude-sonnet-4-6`
  (~40% cheaper), `claude-haiku-4-5` (~80% cheaper). Same harness,
  same 30 scenarios, k=3 trials per arm. The judge stays pinned on
  Opus across all arms — it is part of the instrument, not the
  worker under test.
- **Hypothesis (the structure-dominance question made falsifiable):**
  the harness carries enough of the capability that Sonnet lands
  within 2 items of Opus on pass^k at ~40% of the cost.
- **Decision rule, stated before the run:** Sonnet pass^k ≥ Opus
  pass^k − 0.07 → the production recommendation flips to Sonnet and
  Opus remains the eval-design worker. Haiku is expected to find the
  floor where the model, not the harness, binds — wherever it breaks
  first is a finding, not a failure.
- **Reported per arm:** pass^k / pass@k, per-dim and per-slice rates,
  cost per passed triage, latency percentiles. Each non-Opus arm is a
  new worker epoch: its failures get fresh attribution, never
  back-ported assumptions.
