---
date: 2026-07-31
categories:
  - Data platform
tags:
  - graph
  - benchmarks
  - memgraph
  - profiling
---

# The benchmark that proved my graph database was 40× slower — until it proved me wrong

The responsible default is *not* to add a graph database. It's another
system to run and operate, and a plain Postgres recursive query handles
more traversal work than people tend to assume. A graph store has to
*earn* its place — which means the first move isn't assuming it's faster,
it's measuring whether it's faster at all, on your actual
workload. So I put the graph database and a plain Postgres recursive query
head-to-head on the same data. The first numbers said the graph store was
**40 times slower**. This post is about what I did next, because the
interesting part isn't the result — it's that the result was a lie my own
code was telling me, and the *shape* of the numbers is what gave it away.

<!-- more -->

This is the fourth post about **resgraph**, a mini referential data
platform built in public. The previous phase built a deterministic
generator that emits a realistic stream of cloud-infrastructure events.
This phase gives those events a home: a graph hot store holding the
current state of the world, and the traversal queries that justify using
a graph database at all — "if this host dies, what's affected?"
(blast radius), "why does A depend on B?" (dependency path), "what's
orphaned?" This is where the platform stops being a firehose and becomes
something you can *ask questions of*, and where a claim I'd taken on faith
finally had to face a measurement.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-2-graph-store`](https://github.com/fespino/resgraph/tree/phase-2-graph-store).

## Why a graph store, and the claim under test

The data is a graph: VMs run on hosts, containers run on VMs, load
balancers route to things, each with a direction — edges point from
*dependent* to *dependency*. The blast radius of a host is then just
"everything with a directed path *to* it." In a graph database that's a
single traversal. In a relational database it's a recursive
common-table-expression that joins a table to itself, one level per hop.

The design decision to use a graph store (Memgraph, in this project) had
been made on paper, with a recorded justification. But part of that
justification leaned on traversal performance — and if that's a reason for
taking on a second database, it's a *claim* that owes evidence, not an
axiom you get to assume. The sober position is that a graph database
shouldn't be there at all unless it clearly pulls its weight, so the
burden of proof is on the graph store, and the fair comparison is the
thing you'd otherwise reach for: Postgres with a decent index. So I loaded
the *same* seeded world into both stores — identical graphs, because the
generator is deterministic — and benchmarked the same blast-radius query
at depth 3 and depth 5, across a mix of hub and leaf targets, on worlds of
10,000 and 100,000 resources.

## The result that didn't make sense

| World | Store | Depth | p50 |
|---|---|---|---|
| 100k | Memgraph | 3 | **17.5 ms** |
| 100k | Postgres CTE | 3 | 0.4 ms |
| 100k | Memgraph | 5 | 17.8 ms |
| 100k | Postgres CTE | 5 | 0.4 ms |

Forty times slower. If I'd been in a hurry — if the goal were to confirm a
bias rather than to learn something — this is where I'd have shrugged,
written "Postgres wins at this scale, interesting," and moved on. It would
even have been a *defensible* conclusion. It was also completely wrong,
and one number in that table says so.

## The tell was in the shape, not the size

Look at the two Memgraph rows: **17.5 ms at depth 3, 17.8 ms at depth 5.**
Going from a 3-hop traversal to a 5-hop traversal — meaningfully more
graph to walk — cost basically nothing. That's not what a traversal cost
looks like. If the traversal were the expensive part, deeper would be
slower. The flatness meant the traversal was *cheap*, and something
**constant** — paid once per query regardless of depth — was eating the
17 milliseconds.

That observation is the whole post. A benchmark doesn't just produce a
number; it produces a *shape*, and the shape carries information the
headline number hides. A slower-than-expected result that's also
suspiciously *flat* is not "the graph database is slow." It's "you're
measuring something other than the graph database."

## Isolating the constant with two cheap probes

I didn't guess. I measured two more things, each a few lines:

- A trivial query (`RETURN 1`) — the pure round-trip floor. **0.27 ms.**
  So the protocol overhead is negligible; that's not it.
- A single node lookup by ID — *finding the starting point of the
  traversal*, before any walking happens. **9.4 ms.**

There it was. Just *locating the anchor node* cost 9.4 milliseconds on a
100k-node graph. That's not a lookup; that's a full scan. The database was
reading every node to find the one I asked for.

The cause was a decision from the graph-modeling phase: each resource type
gets its own label and its own index (`:host(id)`, `:vm(id)`, and so on),
because per-type indexes give the query planner selectivity. Good
decision — but it only pays off *if the query names the label*. My
blast-radius query anchored on a label-less pattern, "the node with this
id, whatever type it is." With no label, the per-label index is unusable,
so the database falls back to scanning. I had silently reintroduced the
exact cost the per-type-index decision was supposed to eliminate.

The fix was to derive the label from the id (the id already encodes the
type) and name it in the query — which also had to be validated against
the known types, so it doubles as an injection guard since the label goes
into the query string. One-line idea, dramatic effect:

| Operation | Before | After |
|---|---|---|
| Anchor lookup | 9.4 ms | 0.31 ms |
| Whole blast-radius query | 17.5 ms | **0.2 ms** |

## The result: it's a tie

With the query actually using the index, here's the real comparison:

| World | Store | Depth | p50 |
|---|---|---|---|
| 100k | Memgraph | 3 | 0.2 ms |
| 100k | Postgres CTE | 3 | 0.4 ms |
| 100k | Memgraph | 5 | 0.2 ms |
| 100k | Postgres CTE | 5 | 0.4 ms |

Both sub-millisecond. Not a 40× graph loss, not the comfortable graph win
I originally expected — a **tie**. The story the data actually tells is:
at laptop scale, with worlds that fit in memory and blast radii of a few
dozen nodes, neither the graph's pointer-chasing nor the relational
engine's join-per-level is stressed. The expected divergence — where graph
stores pull ahead on deep traversals over hub-heavy nodes — needs bigger
worlds, deeper queries, or higher fan-out than a 100k-resource fixture
provides.

That is a more useful conclusion than either the wrong "Postgres wins" or
the biased "graph wins," and it forced an honest correction upstream: the
decision to use a graph store now stands on the reasons the benchmark
*does* support — in-memory footprint, instant startup for test cycles,
and the fact that the query language and driver transfer to other graph
databases — and explicitly *not* on "traversal supremacy," which this
scale can't demonstrate. The reversal condition on that decision is now
backed by data instead of assumption.

At this scale, a recursive CTE over an indexed edge table would do the
job — the graph store isn't earning its keep on traversal speed. I'm
keeping it for the non-speed reasons above, and because later phases lean
on graph-native operations (variable-depth traversals, path queries,
algorithms) that get awkward in SQL. That's a bet on future workload, not
a settled win. If those phases don't materialize, dropping the graph store
is the honest move, and the reversal condition says so.

## A smaller lesson hiding in the test suite

One more measure-don't-assume moment. The blast-radius result must be a
*set* of affected nodes, not a *count of paths* — multiple paths can reach
the same dependent, and counting paths instead of nodes is a classic
wrong-metric bug. I wanted a regression test pinning that distinction, so
I needed a case where one node is reachable by two paths.

I went looking for one in the fixture world and there wasn't a clean one —
at that seed, no load balancer happened to route to two things on the same
host. I only found that out by checking. So the test constructs its
diamond explicitly rather than relying on a fixture that happened to have
the shape I assumed. Even "surely the random world contains this obvious
pattern" is an assumption worth measuring before you build a test on it.

## What I'd take to the next project

- **Read the shape, not just the number.** A flat curve where you expected
  a rising one is a louder signal than the magnitude of the number itself.
  The 40× was noise; the 17.5-vs-17.8 flatness was the finding.
- **Isolate before you optimize.** Two throwaway probes — a round-trip
  floor and an anchor lookup — turned "the graph database is slow" into
  "my query doesn't use the index." Neither took more than a minute to
  write.
- **A benchmark can correct a design decision's *rationale*, not just its
  score.** The graph-store choice survived, but the reasons for it
  changed. Letting the measurement rewrite the justification — including
  admitting the benchmark you expected to run was a tie — is the whole
  point of running it.

The graph store now holds the world and answers questions about it in a
fifth of a millisecond. Next in this thread: the cold-history half of the
story — snapshotting this same world to an Iceberg store so you can ask
those questions not just about *now* but about any point in the past.
That phase isn't built yet, so that post comes when the numbers do.
