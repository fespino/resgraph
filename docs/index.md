# resgraph

A mini referential data platform, built in public. A synthetic
cloud-infrastructure world streams updates into a graph hot store and
an Iceberg cold store, queryable by traversal and time travel, with
agents, serving, and compliance layers on top. Every benchmark number
ships with hardware and methodology. The code lives on
[GitHub](https://github.com/fespino/resgraph).

## Writing

Notes written as the work was done — see also the
[writing index](writing/index.md) with excerpts, or browse by tag from
any post.

- [Anatomy of a seven-billion-dollar paragraph](blog/posts/19-anatomy-of-a-seven-billion-dollar-paragraph.md) — 2026-08-18
- [Institutional memory is a log-structured store](blog/posts/18-institutional-memory-log-structured.md) — 2026-08-16
- [Day-2 of serving: the drill's findings became the backlog](blog/posts/17-day-2-of-serving.md) — 2026-08-16
- [Cache the investigation, never the answer](blog/posts/16-cache-the-investigation-never-the-answer.md) — 2026-08-16
- [Two backends is failover with telemetry, not load balancing](blog/posts/15-two-backends-is-failover-with-telemetry.md) — 2026-08-16
- [The eval that doesn't care where a model runs](blog/posts/14-the-eval-that-doesnt-care-where-a-model-runs.md) — 2026-08-16
- [The drill that measured nothing](blog/posts/13-the-drill-that-measured-nothing.md) — 2026-08-16
- [The controls come before the capability](blog/posts/12-controls-before-capability.md) — 2026-08-16
- [Who grades the graders?](blog/posts/11-who-grades-the-graders.md) — 2026-08-04
- [Goodhart's law operates inside a prompt](blog/posts/10-goodhart-inside-a-prompt.md) — 2026-08-04
- [Ground truth first, judge last](blog/posts/09-ground-truth-first-judge-last.md) — 2026-08-04
- [An MCP server is an API with opinions](blog/posts/08-an-mcp-server-is-an-api-with-opinions.md) — 2026-08-04
- [Wide events, derived SLOs, and the drill that closed Part I](blog/posts/07-wide-events-and-the-capstone-drill.md) — 2026-08-03
- [The query layer: predicate and projection push-down across two stores](blog/posts/06-pushdown-across-two-stores.md) — 2026-08-02
- [Cold history: time travel runs on event time, not commit time](blog/posts/05-cold-history-two-clocks.md) — 2026-08-01
- [One watermark, three guarantees](blog/posts/04-one-watermark-three-guarantees.md) — 2026-07-31
- [The benchmark that proved my graph database was 40× slower — until it proved me wrong](blog/posts/03-hot-graph-honest-benchmark.md) — 2026-07-31
- [A deterministic synthetic cloud, and a 45× lesson in measuring before believing](blog/posts/02-a-deterministic-synthetic-cloud.md) — 2026-07-29
- [Decisions with reversal conditions: a spec that fights back](blog/posts/01-decisions-with-reversal-conditions.md) — 2026-07-27
- [Security from the first commit, not as an afterthought](blog/posts/00-security-from-the-first-commit.md) — 2026-07-27

## Engineering docs

- [Security posture](security-posture.md)
- [Stream contract](stream-contract.md)
- [Planner vocabulary](planner-vocabulary.md)
- [Capacity](capacity.md)
- [Prompt audit](prompt-audit.md)
- [Evals compaction runbook](evals-compaction-runbook.md)
- [Drills runbook](drills/README.md)
- [Sentinel threat model](sentinel/threat-model.md)
