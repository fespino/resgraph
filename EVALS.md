# EVALS — the analyst's iteration log

The quality bar lives in `docs/discovery/incident-triage.md`, written
before any harness code. This file records every run against it:
baseline first and unpolished, then one fix per iteration, each
pre-registered (hypothesis → single change → predicted effect →
invalidating result) before its run. The committed baseline is
`evals/baseline.json`; it refreshes only via labeled commits.

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

## Iteration 1 — pre-registered 2026-08-03, run pending

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

## Iteration 2 — pre-registered 2026-08-03, queued behind the halt

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

Honesty (0/6 controls) is the next-biggest bucket after the halt
clears and is prompt-side work on the abstention rules; it gets its
own pre-registration when it comes up.
