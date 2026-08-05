# Harnessability review — phase 9 (2026-08-05)

Method: [lopopolo's repository-review playbook](https://github.com/lopopolo/harness-engineering/blob/trunk/playbooks/repository-review.md)
— inspect the agent-repository system as trajectories (nine stages),
review the ownership boundaries, run at least one real journey, and
emit findings ordered by consequence. Adopted via #143 (item 8); the
playbook was meant to open the phase and ran after the first three
build PRs instead — late, recorded as such.

Two agents live in this system and the review covers both: the
**analyst** (the triage agent the platform runs) and the **build
agent** (the coding agent that engineers this repo through PRs).

## The journeys inspected

Static inspection is insufficient by the playbook's own rule, so:

- **Analyst, paid and recorded:** the k=3 certification run
  (`evals/runs/20260803T221121Z.jsonl`, 90 rows — including a
  mid-run network drop recovered by `--resume` with zero re-paid
  rows) and iteration 9 (`evals/runs/20260805T135538Z.jsonl`).
  Real API, real stores, budgets and brakes exercised in anger.
- **Safe runtime, end-to-end:** plan render → typed approval (with a
  skip) → step-machine execution → audit timeline / `--touched` /
  `--trace` / `--verify`, driven through the real CLI against a real
  store file; plus the injection-shaped run
  (`test_injection_distorts_the_proposal_never_the_execution`),
  where a model obeying injected instructions reaches an error
  outcome, not an execution.

## The nine stages, answered

1. **Task classification** — single task class (alert → triage);
   the alert payload and world summary frame it. No routing to
   misclassify. Pass.
2. **Root guidance → domain context** — analyst: prefix (identity,
   triage-discipline skill, output contract) with a committed
   PREFIX/SUFFIX audit table. Build agent: INDEX.md map + SPEC.md
   decision log. Pass.
3. **Find the existing owner** — TOOL_REGISTRY is the single source
   for all three tool surfaces; decisions carry D-numbers; INDEX.md
   answers "where is the thing that does X". Pass.
4. **Reproduce and observe without a human intermediary** — seeded
   generator + compose stores + eval runner make any scenario
   reproducible from code. The analyst cannot observe its own past
   runs (no tool over the audit store) — a deliberate context-budget
   choice today, becomes a real question when memory work arrives.
5. **One domain model, one source of truth** — D2 schema, registry
   canonicality, SPEC as the only decision log. The exception is
   finding F2 below: run identity exists twice.
6. **Proof at the right boundary** — mostly held (stdio protocol
   tests, cross-store golden test). Two recent counterexamples both
   got corrected in-phase: the audit CLI shipped at 0% coverage
   behind a manual smoke (#142's coverage drop), and the proposal
   boundary was architecture-without-a-test until #143 item 5.
7. **Review, CI, conflicts, delivery** — build agent: five-leg CI,
   branch protection, issue → branch → PR. Finding F1: the local
   gate was not repo-owned.
8. **Which operation needs human judgment or new authority** — the
   typed approval gate (D26), merge decisions and paid runs held by
   the maintainer, `--max-cost` brakes. Pass.
9. **Durable improvement for the next run** — SPEC decisions with
   reversal conditions, EVALS.md ledger with corrections, mined
   regression items, this review. Pass.

## Ownership boundaries

Context/routing (prompts module owns words, harness owns order — D23);
capabilities (registry + D26 composition rule); domain/architecture
(D2/D19); execution+proof (graders vs planted truth — D24/D25);
feedback (eval gate arrives with D29); dependencies (pinned digests,
osv, Dependabot); delivery+authority (D26 gate, branch protection);
proportionality (laptop scale declared everywhere numbers appear).

## Findings, ordered by consequence

**F1 — the local gate was not repo-owned.**
*Risk:* commits verified against a partial gate; CI becomes the first
full check. *Evidence:* three CI round-trips (ruff format twice —
PR #122, #135; bandit once — PR #142), boundary: delivery. *Why
existing proof missed it:* CI is the enforcing owner but only
post-push; the five-leg checklist lived outside the repo. *Root
correction:* `scripts/gate.sh` runs the same five legs locally;
CLAUDE.md points at it. *Redundant machinery:* none to remove.
**Corrected in this PR.**

**F2 — run identity exists twice, with no join key.**
*Risk:* an incident question spanning the eval ledger and the audit
trail ("which eval run produced this remediation proposal?") cannot
be answered from either store. *Evidence:* eval run rows are keyed by
timestamp file + row fingerprint (D24); audit runs by caller-chosen
`run_id` (D27); nothing constrains them to agree. Boundary:
domain/one-source-of-truth. *Why missed:* the two stores arrived in
different phases with different owners. *Root correction:* when the
eval runner gains audit wiring (D29 work), it passes its own run
identifier as the audit `run_id` — one key. **Recorded as a
constraint on #139.**

**F3 — the gate protects a tool that does not exist yet.**
*Risk:* none today — this is the instrument-before-subject ordering
working as intended — but the phase is not done until the subject
arrives: there is no `apply_remediation` executor emitting
compensating updates into the ingest stream, and no `triage` CLI
driving alert → report → proposal → gate end-to-end from one entry
point. *Evidence:* `resgraph-analyst` has one subcommand (`audit`);
the approval flow is a library whose only caller is tests. Boundary:
capabilities/delivery. *Why missed:* #138 deliberately scoped the
protocol before the tool. *Root correction:* the executor + triage
CLI as the closing build item of the D26–D28 arc. **Filed as its own
issue.**

No redundant machinery was identified for removal this round; the
phase is young enough that the accumulation problem hasn't started.
