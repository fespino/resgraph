# resgraph

[![CI](https://github.com/fespino/resgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/fespino/resgraph/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/fespino/resgraph/badge)](https://scorecard.dev/viewer/?uri=github.com/fespino/resgraph)

A mini referential data platform, built in public: a synthetic cloud-
infrastructure world streams updates into a graph hot store and an
Iceberg cold store, queryable by traversal and time travel — with agents,
serving, and compliance layers built on top. Honest benchmarks only:
every number ships with hardware + methodology.

Runs on any OCI runtime — tested with Docker/OrbStack; Podman-compatible
(rootless).

```mermaid
flowchart LR
  G[generator\nsynthetic world] -->|D2 messages| S[(Redis Stream)]
  S --> I[ingest\nbatch + idempotent D3]
  I --> M[(Memgraph\nhot graph)]
  I --> B[(Iceberg\nhistory)]
  M --> Q[query layer\ntraversals]
  B --> Q2[time travel\nDuckDB]
  Q --> A[analyst agent + MCP]
  Q2 --> A
```

Status: phase 0 — decision log + wire schema. Each increment lands via
issue → PR, citing the SPEC decisions (D-numbers) it implements.
