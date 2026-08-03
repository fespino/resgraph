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

## Iteration 5 — pre-registered 2026-08-03, run pending

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
