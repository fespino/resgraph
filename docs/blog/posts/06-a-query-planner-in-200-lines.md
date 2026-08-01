---
date: 2026-08-02
categories:
  - Data platform
tags:
  - query-engine
  - planner
  - push-down
  - benchmarks
  - api
---

# A query planner in 200 lines — and the half I didn't build

The platform now has two stores that disagree about everything: a
graph store that speaks Cypher and knows only the present, and an
Iceberg/DuckDB cold store that speaks SQL and knows only the past.
The question users actually ask — *blast radius of this host, as of
last Tuesday* — spans both. This post is about the thin layer that
answers it: a filter DSL, a placement table, and a lazy plan, ~200
lines that earn the vocabulary of a real query engine. It is also
about the embarrassing part: I built predicate push-down with tests,
benchmarks, and a documentation page mapping it to the literature —
and the literature, when I finally read it properly, pointed out I
had built exactly half of the idea. The missing half was worth 33% of
the phase's headline latency.

<!-- more -->

This is the seventh post about **resgraph**, a mini referential data
platform built in public. The pipeline: a deterministic generator
streams infrastructure updates, a consumer applies them idempotently
into a graph store, a cold store keeps the full history in Iceberg,
and event-time travel reconstructs the world as of any moment. This
phase closes the data-foundation cycle with one HTTP surface over
both stores (D15 in the spec) and the mini planner behind it (D16).

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-5-query-layer`](https://github.com/fespino/resgraph/tree/phase-5-query-layer).

## The endpoint table is the budget

The API is six fixed endpoints — current resource, live blast radius,
per-resource history, world-as-of-T, diff, and the composite
blast-radius-as-of-T. Two alternatives were rejected on the record,
and the rejections matter more than the endpoints. Raw Cypher/SQL
passthrough: maximum power, but it couples every caller to store
choice and hands the coming agent phase an injection surface instead
of a tool surface. GraphQL: resolver flexibility is precisely the
unplanned query shape that result budgets exist to prevent. A fixed
endpoint table *is* the budget — every list response caps at 1,000
rows and says so (`truncated`, `total_count`), and every response
carries `source: hot | cold | composite`, because the previous post's
two-clocks lesson doesn't stop applying at the HTTP boundary: a
caller must be able to tell which store, and therefore which clock,
produced an answer.

## The planner: a table lookup wearing engine vocabulary

The filter grammar is conjunctions only — `type=vm AND
attrs.zone=z1` — parsed once at the boundary into typed predicates;
OR, grouping, and functions are refused by name. Planning is then a
placement decision per predicate: type and known-attribute filters
compile into the store best able to evaluate them (Cypher `WHERE` on
the live route, DuckDB `WHERE` on the cold route); anything neither
store claims becomes a **residual filter**, applied in Python and
flagged as a top-level field of the plan. The set of known attribute
fields isn't maintained by hand — it derives from the generator's own
attribute pools, the same table-is-code discipline the spec has used
since the topology table, so the placement table cannot drift from
the world's actual schema.

Two properties are load-bearing enough to be tests rather than
prose. First, plans are **lazy**: `plan()` returns data, nothing
touches a store until execution, and `?explain=true` returns the plan
without executing. The test for that runs the API with *no stores
running at all* and asserts the driver and catalog were never
created — the difference between claiming lazy evaluation and proving
it is one fixture. Second, residuals are **visible**: ask for
`attrs.frobnitz=9` and the plan says, in so many words, that a Python
step will filter what the stores couldn't — a slow query explains
itself, which is the property production EXPLAIN output exists to
provide.

## The composite, and the bug I didn't write

The composite route reconstructs the as-of-T world from the cold
store, builds an in-memory dependency graph, and BFS-traverses it
with the same direction and depth cap as the live route. The subtle
part is what the filter *means*. A predicate on a blast-radius query
filters the **affected set** — but the traversal needs the **full**
topology, because the path from a z1 VM to your host may run through
a z2 load balancer. The naive implementation — push the filter into
the reconstruction, traverse what's left — type-checks, runs
*faster*, and silently drops every path through a non-matching node.
So pushed predicates on the composite run as a computed match column
in the same DuckDB scan, never as a scan reduction, and the affected
set intersects with the matches after the traversal.

What kept this honest is the phase's golden test: feed both stores
the identical message sequence through their own write paths, then
assert the composite at T=now equals the live blast radius across 25
sampled roots — then again under push-down filters, including one
(`type=container`) whose matches are reachable only *through*
non-matching nodes. The two routes share no
query code, so agreement is a real cross-store proof — and it is
exactly the test the faster-but-wrong version fails.

## The numbers, and what they actually say

| Measure | World | Result |
|---|---|---|
| composite as-of blast radius, p50 | 10k res / 1M events | **0.250 s** (reconstruct 0.24, traverse 0.009) |
| `/world` pushed vs forced-residual, p50 | 1M events | **0.145 s vs 0.367 s (2.5×)** |
| `/world` pushed vs forced-residual, p50 | 10k events | 0.025 s vs 0.033 s (1.3×) |
| live blast radius, end-to-end p50 | 5k res + churn | **2.5 ms** |
| `plan()` + `explain()`, p50 | — | 0.012 ms |

Two findings beat the raw numbers. The composite is **a
reconstruction with a rounding error attached**: cold `state_at` is
94–97% of the total at every size; the in-memory traverse never
exceeds ~10 ms even at 30k alive resources. Optimizing this query
means optimizing reconstruction — the ephemeral-graph half of the
design never becomes the problem at this scale.

The second finding took me longer to see. The push-down delta *grows*
with scale — 1.3× at ten thousand events, 2.5× at a million. The
residual path doesn't lose because Python compares values slowly; it
loses because every row must cross the Arrow→Python boundary and
through a JSON parse before the filter can look at it, and that tax
scales with the world while the pushed filter's cost scales with the
matches. **Push-down is less about where the comparison runs than
about how much data crosses the engine boundary.** Which set up the
lesson I didn't see coming.

## The book review that found the missing half

After the phase shipped I read Andy Grove's
[*How Query Engines Work*](https://howqueryengineswork.com/) cover to
relevant cover — the same move as reviewing the cold store against
Kreps' log essay: build first, then let a canonical text grade the
result. The grade came back specific. The book's data-source
contract is `scan(projection)` — *column* pruning is the baseline,
stated before predicates ever appear, and its optimizer's first
implemented rule is projection push-down. My planner pushed
predicates diligently and returned every column: the composite
JSON-parsed the attributes of thirty thousand reconstructed rows to
return four rows of ids and types. My own benchmark had already named
the boundary tax as the dominant cost. I had measured the disease and
built the cure for half of it.

The fix is the book's contract, applied: `state_at` takes a
projection (which data columns to return) plus the columns the
predicate reads (scanned and evaluated, never returned), and both
prune at the Iceberg scan itself, so unrequested columns never cross
the boundary at all. The composite now travels with relationships and
types only, picking up attributes just when a residual predicate
must read them. Measured at a million events: composite p50 **0.371 s
→ 0.250 s**. The general lesson is the one I'd repeat to anyone
building an optimization from first principles: **you notice the
optimization you built and stay blind to its dual.** Predicate
push-down and projection push-down are one idea — ship less data —
sliced two ways, and it took a book naming them side by side for me
to see the second slice was missing.

The same review settled a design doubt in the other direction. The
book insists on separating logical plans from physical plans, for
three reasons: choosing among algorithms per operator, adapting to
execution environments, and cost-based selection among candidate
plans. All three are absent here by design — one algorithm per route,
one environment, and a spec-recorded refusal to grow statistics. So
this planner's `Plan` is a logical plan with the physical choice
pre-made, and the spec now says so, with the trigger for revisiting:
the first placement decision that *needs* statistics to make is the
moment a physical layer earns its existence — and, per the decision
log, also the moment to adopt a federated engine rather than build
one. The book's own teaching engine quietly concedes the point: its
planner makes fixed physical choices too. The separation is pedagogy
until there are real alternatives to choose between.

## What I'd take to the next project

- **Fixed endpoints are a security and budget decision, not a
  convenience.** The rejection of passthrough is what makes result
  caps enforceable and gives the coming agent phase a tool surface
  instead of an injection surface.
- **Make laziness and residuals observable properties.** "Explain
  doesn't touch stores" is a test with no stores running; "unclaimed
  predicates are visible" is a plan field. Both cost almost nothing
  and turn design claims into regressions-waiting-to-fail.
- **Push-down is a pair.** Predicates and projection are the same
  idea; if you built one, go find out where the columns are going.
  The delta grows with scale, and it hides in serialization, not in
  the comparison.
- **Let a canonical text review the build.** Kreps found the
  subscriber-bootstrap gap in the cold store; Grove found the
  projection gap here. A book can't run your benchmarks, but it can
  name the half of an idea you didn't know had halves.
- **When a filtered traversal is involved, decide what the filter
  *means* before deciding where it runs.** The fast version that
  filters the topology is wrong in a way only a cross-store golden
  test will catch.

The data foundation is now closed: generate, stream, apply, remember,
and query — one surface over both stores, every budget measured.
Ahead: the part this platform was built for — wrapping these
endpoints as tools an agent can be trusted with, and finding out what
an honest evaluation of that agent looks like.
