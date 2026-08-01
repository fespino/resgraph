# Repo map

- SPEC.md — decisions (D-NN) + phase contracts
- src/resgraph/schema.py — D2 update-message schema (pydantic)
- src/resgraph/gen/ — deterministic world generator (D5–D7): world, churn, sinks, CLI
- BENCHMARKS.md — measured numbers with hardware + method (D4)
- docs/stream-contract.md — why consumers must not assume referential integrity
- tests/ — schema + per-component tests
- compose.yaml — local stores (grows with the platform)
- .github/workflows/ — CI + security gates (see docs/security-posture.md)
- docs/security-posture.md — every security control and why it exists
- src/resgraph/graph/ — hot store client, DDL, snapshot loader, traversal queries (D8–D9),
  idempotent ingest apply (D3/D10)
- src/resgraph/consumer.py — generic stream consumer loop (pending-first recovery, ack-after-apply)
- src/resgraph/cold/ — Iceberg cold store: catalog/tables/appends, CLI (D11–D13)
- benchmarks/ — measurement scripts (methods in BENCHMARKS.md)
