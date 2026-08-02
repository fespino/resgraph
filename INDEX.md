# Repo map

- SPEC.md — decisions (D-NN) + phase contracts
- src/resgraph/schema.py — D2 update-message schema (pydantic)
- src/resgraph/gen/ — deterministic world generator (D5–D7) + causal-scenario planting (D25): world, churn, scenarios, sinks, CLI
- src/resgraph/analyst/ — the triage agent (D22–D23): harness loop, report models, registry-derived Anthropic tool surface
- BENCHMARKS.md — measured numbers with hardware + method (D4)
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
- observability/ — prometheus scrape config, D18 SLO rules (+ promtool tests),
  grafana provisioning + the resgraph-overview dashboard as JSON
- scripts/drill-hotstore-loss.sh — the INC-001 chaos drill, scripted
- docs/incidents/ — incident notes (induced drills labeled as such)
- benchmarks/ — measurement scripts (methods in BENCHMARKS.md)
- src/resgraph/tools/ — canonical tool layer (D19/D20): registry as the
  single source of truth, budgets, refs+fetch shaping, HTTP projection
- src/resgraph/mcp/ — MCP server over the registry (stdio), server card,
  skills-as-prompts loader (D21)
- skills/ — investigation playbooks (SKILL.md, validated at startup)
- .mcp.json — Claude Code/Desktop wiring for the resgraph MCP server
