# Repo map

- SPEC.md — decisions (D-NN) + phase contracts
- src/resgraph/schema.py — D2 update-message schema (pydantic)
- src/resgraph/gen/ — deterministic world generator (D5–D7) + causal-scenario planting (D25): world, churn, scenarios, sinks, CLI
- src/resgraph/analyst/ — the triage agent (D22–D23) and its safe runtime (D26–D28): harness loop, report models, prompts + cache audit, registry-derived Anthropic tool surface, remediation step machine, approval gate, the privileged `apply_remediation` executor (compensating D2 updates onto the ingest stream), audit store + the `resgraph-analyst` CLI (`triage`, `audit`)
- src/resgraph/evals/ — the analyst's eval harness (D24): deterministic graders, pinned judge, store-isolated runner, report, judge spend breaker (D29a), provider adapter for a pluggable worker (D29c); datasets + runs live in evals/
- BENCHMARKS.md — measured numbers with hardware + method (D4)
- EVALS.md — the analyst's iteration log (D24): protocol rules, per-run
  pre-registrations and outcomes, conclusions with receipts, honest review
- evals/ — eval artifacts: scenarios/ (committed recipes + the
  trace-mining sanitization checklist), runs/ (envpinned
  row files), baseline.json, models.yaml (named model setups, --worker/--judge),
  meta/ (grader mutation gate, also a CI step)
- docs/discovery/ — problem-discovery memos written before code (the
  quality bar's git history is the witness)
- docs/prompt-audit.md — PREFIX/SUFFIX verdict table + cache diagnosis
  branches (D23)
- docs/blog/ — published-post assets
- docs/stream-contract.md — why consumers must not assume referential integrity
- tests/ — schema + per-component tests
- compose.yaml — local stores (grows with the platform)
- .github/workflows/ — CI + security gates (see docs/security-posture.md)
- docs/security-posture.md — every security control and why it exists
- src/resgraph/graph/ — hot store client, DDL, snapshot loader, traversal queries (D8–D9),
  idempotent ingest apply (D3/D10)
- src/resgraph/consumer.py — generic stream consumer loop (pending-first recovery,
  ack-after-apply, apply-failure containment: retry/split/DLQ per D14 addendum)
- src/resgraph/cold/ — Iceberg cold store: catalog/tables/appends, CLI (D11–D13)
- src/resgraph/query/ — filter DSL, mini planner, execution over both stores (D16)
- src/resgraph/api/ — FastAPI surface: budgets, labeled sources, lazy explain (D15)
- sql/cold_semantics.sql — D13 event-time semantics as portable SQL for any engine
  reading the Iceberg table directly (D15 addendum)
- docs/planner-vocabulary.md — the mini planner mapped to query-engine terms
- src/resgraph/obs.py — telemetry (D17): wide-event sink + OTel metrics
- src/resgraph/reconcile.py — hot vs cold vs oracle full-state comparison
- observability/ — prometheus scrape config, D18 SLO rules + D29b agent SLO rules (+ promtool tests),
  grafana provisioning + the resgraph-overview dashboard as JSON
- scripts/drill-hotstore-loss.sh — the INC-001 chaos drill, scripted
- scripts/drill-analyst-degraded.sh — the INC-002 drill: the hot store dies mid-triage, honesty graded
- docs/drills/ — the drill runbook: causal chain, pre-mortem, pilot gate,
  postmortem; templates to point at, plus this phase's pre-mortem
- docs/incidents/ — incident notes (induced drills labeled as such)
- docs/reviews/ — recorded system reviews (harnessability, checklist walks)
- benchmarks/ — measurement scripts (methods in BENCHMARKS.md)
- src/resgraph/tools/ — canonical tool layer (D19/D20): registry as the
  single source of truth, budgets, refs+fetch shaping, HTTP projection
- src/resgraph/mcp/ — MCP server over the registry (stdio), server card,
  skills-as-prompts loader (D21)
- src/resgraph/gateway/ — serving gateway (D30–D33): precedence router with
  recorded source, dispatch policy (queues/health/EWMA), stream relay +
  accounting, `resgraph-gateway serve` with health probes; caches to follow
- skills/ — investigation playbooks (SKILL.md, validated at startup)
- .mcp.json — Claude Code/Desktop wiring for the resgraph MCP server
