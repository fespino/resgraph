# Problem discovery: incident triage

Written before any harness code, deliberately: this memo is the quality
contract the agent will be built *inside*. If the bar below quietly
bends to match what the agent happens to do, the git history of this
file is the witness.

## The process today (human steps, enumerated)

An alert fires on resource X. The on-call engineer:

1. Opens the dashboard/graph and pulls X's current state.
2. Walks X's dependencies — what does X run on, route to, attach to.
3. Asks "what changed recently near X?" — diffs the window around the
   alert, usually too wide on the first try.
4. Reads change history for each candidate that moved.
5. Builds a mental causal chain from some change to X's symptom.
6. Writes a triage note naming ranked suspects with evidence — or,
   rarely and reluctantly, "nothing obvious."

Steps 2–5 are exactly the platform's query surface (`blast_radius`,
`world_diff`, `resource_history`, `fetch_resource`, `dependency_path`).
The human contribution is sequencing, judgment about where to look
next, and the discipline to cite evidence rather than intuition.

## Cost (assumed, laptop-scale honest)

Assumed shape for a small platform team: 15–40 minutes of senior
attention per triage, 3–8 times a week — call it 2–4 hours/week of the
most interrupt-expensive kind of work, since a triage lands mid-focus
and costs a context switch on top of its duration. These are template
numbers, not a measured business case; the shape (frequency × duration
× seniority + interrupt tax) is what transfers to any real deployment.
What makes automation worth attempting is not the hours — it is that
the process is *checkable*: every claim a triage makes is verifiable
against the graph and the log.

## The quality bar ("good" defined measurably, before the first run)

- **Found:** the planted causal change appears in the top-3 suspects
  in ≥ 80% of scenarios; top-1 in ≥ 60%.
- **Fabricated evidence rate = 0.** Not low — zero. Every mechanism
  edge a suspect cites must have existed in the graph at incident
  time; every cited change must exist in the event log. A triage tool
  that invents an edge is worse than no tool: it spends the on-call's
  trust and their time. Fabrication is disqualifying, not a quality
  trade-off, and any run where it appears stops iteration until it is
  zero again.
- **Honesty on controls ≥ 90%:** on scenarios with no planted cause,
  the agent concludes "no confident candidate." That answer *passes*.
  A high-confidence wrong answer scores worse than an honest miss on
  every dimension.
- **Calibration means something:** the agent's high/medium/low
  confidence must track its own empirical accuracy (high beats medium
  beats low), or the field is decoration and the report says so.
- **Budgets:** p95 wall-clock and cost per run inside the committed
  baseline (numbers land with the baseline JSON at run 1); token-
  weighted cache hit rate ≥ 0.9 on multi-turn runs.
  *Amended 2026-08-03 (EVALS.md iteration 2, SPEC correction row):
  the cache gate is the uncached re-read fraction ≤ 0.1. The 0.9
  floor penalized one-time cache writes, which are cost every new
  token owes, not waste; the bar changes here, dated, because this
  file's history is the witness that it never bends quietly.*

### Coverage statement (what the bar deliberately excludes)

The scenario taxonomy at v1 covers: direct-dependency causes,
transitive causes (depth 2–3), deleted-resource causes, noisy windows,
ambiguous dual-candidate scenarios, decoy scenarios (a seductive
non-causal confounder planted beside the real cause), and no-cause
controls. Generator-expressible shapes deliberately excluded at v1,
with reasons:

- **Multi-cause incidents** (two interacting planted faults) — the
  decomposition experiment's admission evidence; excluded from the
  base bar so that experiment has a clean before/after.
- **Phantom-node mechanism paths** (cause routed through a node that
  was never upserted) — the graph handles phantoms, but grading a path
  through a phantom needs a grader decision not yet made; recorded
  here so its absence is a choice, not a blind spot.
- **Burst-window causes** (cause inside a churn burst that saturates
  the diff) — deferred until the noisy-window slice is passing; a
  harder variant of a slice that must work first.

## Guardrails (the harness iterates inside these; they do not move)

- **Read-only surface:** the agent sees the tool registry's read
  tools; nothing mutating is exposed anywhere in the loop.
- **Hard budgets in the harness, not the prompt:** a maximum tool-call
  count and a token ceiling per run. Exhaustion is not an error: the
  agent must conclude with what it has and mark the run `degraded`.
- **Referential honesty at the boundary:** a report citing a resource
  the run never fetched fails validation before any grader sees it.
- **Full run audit:** every run records model, git ref, environment,
  tool calls, tokens, cost — the phase that hardens this into
  production-grade audit comes next; the record shape starts here.
