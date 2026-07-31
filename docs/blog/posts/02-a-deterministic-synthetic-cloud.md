---
date: 2026-07-29
categories:
  - Data platform
tags:
  - generator
  - benchmarks
  - profiling
  - determinism
---

# A deterministic synthetic cloud, and a 45× lesson in measuring before believing

You cannot measure a data platform without data. That sounds obvious
until you try it: real infrastructure data is proprietary, messy, and
impossible to replay; hand-written fixtures are too small and too tidy to
stress anything. Before I could benchmark an ingest, a graph store, or an
agent, I needed a firehose of *realistic, reproducible* events. So the
first real component of resgraph isn't a store or a query engine — it's a
world generator. **Ground truth is a feature you build first.**

<!-- more -->

This is the third post about **resgraph**, a mini referential data
platform built in public, and the first one with real numbers to argue
about. The generator (`resgraph-gen`) seeds a plausible cloud-
infrastructure world — hosts, VMs, containers, databases, load balancers
— and emits an endless stream of update messages describing how that
world changes. It's the single most reused component in the project: it's
the load-test driver for the ingest, and later it's the *ground-truth
factory* for evaluating agents, because a world you generated from a seed
is a world whose correct answers you already know. Everything downstream
leans on it, which is exactly why it had to earn its numbers.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-1-generator`](https://github.com/fespino/resgraph/tree/phase-1-generator).

## What "realistic and reproducible" actually requires

Two properties are in tension, and both are non-negotiable.

**Reproducible.** Given the same seed and the same flags, the generator
must emit a *byte-identical* stream — on the interpreter version the
repo pins. The boundary matters: Python only guarantees cross-version
stability for some of `random`'s methods, so the pinned
`.python-version` is part of the determinism contract, not a
convenience. Within that boundary, reproducibility is what makes
benchmarks comparable across runs and machines, and what makes agent
evaluations possible (plant a known fault, check the agent finds it).
Achieving it means a single seeded random source owns *all*
randomness, iteration is always sorted (never dependent on dictionary
insertion order), and — the subtle one — time is *simulated*, not
wall-clock. The generator's clock starts at a fixed epoch and advances by
drawing inter-arrival times from the seeded random source. Wall-clock
time never appears in a message, because wall-clock would make every run
different and every benchmark a lie. Throttling the output rate is
allowed, but it changes *when* messages emit, never *what* they contain —
and a test asserts exactly that.

**Realistic.** Real inventories aren't uniform. A small fraction of
resources are "hot" and churn constantly while most sit quiet. The
generator bakes this in: 5% of resources receive 80% of the updates. This
matters because uniform churn is a lie that *flatters caches* — a later
phase measuring cache behavior under uniform load would get an
optimistic, useless number. The skew is a knob so future phases can crank
it to something nastier than real clouds and see what breaks.

There's also a topology contract — VMs run on hosts, containers run on
VMs, load balancers route to VMs or containers, each with cardinality
bounds — so the graph the generator produces is traversable and its
blast-radius questions have meaningful answers. All of this is verified
with property-based tests that run across random seeds: byte-identical
streams, strictly increasing sequence numbers, every relationship target
alive at emit time, and the topology bounds respected.

## Inside the churn engine

The component that turns the static world into a live stream is a
single class, and its design fits in five decisions worth stealing.

**One dice roll decides the operation.** Every event starts with one
uniform draw, partitioned into bands: 3% delete, 5% create, 12%
relationship rewiring, 80% attribute updates. The proportions mirror a
real inventory — most events are boring attribute flaps; rewiring is
rarer; create and delete are rare. And create (5%) deliberately
outweighs delete (3%), so the world *grows* over a long run — which is
not an accident, it's the mechanism behind a benchmark finding later in
this post.

**Time advances by drawing gaps, not by looking at a clock.** The
simulated clock steps forward by exponentially distributed
inter-arrival times from the seeded source — a Poisson-style event
process. Bursts work the same way: configure a window ("first 5
seconds of every simulated minute, 10× the rate") and it's computed
from *world time*, so the spike lands at the identical spot in every
run. Deterministic load spikes, for stress-testing consumers
reproducibly.

!!! note "Why exponential gaps? A short theory detour"
    A **Poisson process** is the standard model for events that arrive
    independently at some average rate λ — radioactive decays, calls
    hitting a switchboard, updates hitting an inventory. It's
    characterized by one property: **memorylessness**. The chance of an
    event in the next microsecond doesn't depend on how long you've
    already waited; the process carries no state between events.

    That property forces the shape of the gaps. If arrivals are
    memoryless, the waiting time between consecutive events *must*
    follow the exponential distribution — it's the only continuous
    distribution with no memory. Which yields the implementation: you
    don't simulate a rate, you **draw the next gap** from
    `Exponential(λ)` (mean gap = 1/λ) and add it to the clock. One
    random draw per event, no ticking, no discretization error.

    Two consequences the generator leans on:

    - **Realistic clumping for free.** Exponential gaps are mostly
      short with occasional long ones, so events arrive in ragged
      clusters separated by lulls — what production traffic looks like
      — rather than the metronome tick a fixed interval would produce.
      A uniform gap is exactly as unrealistic as uniform churn.
    - **Bursts are just a time-varying λ.** Making the rate a function
      of world time (10× inside a window, 1× outside) turns the model
      into an *inhomogeneous* Poisson process — the textbook way to
      model rush hour on top of baseline traffic. The implementation
      barely changes: check which window `now` falls in, draw the gap
      from that window's rate.

    The model has known limits: real infrastructure events aren't
    perfectly independent (an autoscaler reacting to a host failure is
    a *cascade*, which Poisson won't produce). For a load generator
    that's acceptable — the goal is realistic arrival texture at a
    controlled rate, not a causal model of incidents. If a later phase
    needs cascades, that's a self-exciting (Hawkes) process, and it
    would earn its own decision entry.

**Deletions revive.** When the engine creates a resource, 30% of the
time it resurrects a previously deleted id instead of minting a fresh
one. That exists for one reason: delete-then-recreate is the awkward
case the ingest's tombstone logic has to survive, so the generator
*manufactures* it constantly instead of hoping it shows up. If a
downstream component mishandles revival, the stream finds out within
seconds, not in production.

**Floors protect the topology.** Types that other types depend on —
hosts, VMs, security groups — are never deleted below a minimum count.
Without the floor, an unlucky run could delete the last host and leave
resource creation with no valid target, violating the topology's own
cardinality bounds. The invariants aren't just tested; the engine is
built so it *can't* emit a world that breaks them.

**Searches are bounded, then degrade.** Picking a deletable resource
(or one that owns edges to rewire) tries at most eight skewed draws,
then falls back to an attribute update. An unbounded retry loop would
consume a data-dependent number of random draws — and any
data-dependent draw count makes the stream's determinism fragile.
Capping the search keeps the random stream's structure stable and the
engine total: it always returns *a* message.

One subtlety worth knowing before the later phases: when a resource is
deleted, the world silently repairs its dependents' dangling edges —
but emits **no repair messages**. The stream therefore legitimately
contains edges pointing at deleted resources. That's a contract, not a
bug: consumers must tolerate dangling references, and the graph
store's phantom-node mechanics exist precisely because the generator
refuses to pretend the stream is referentially clean.

## The benchmark that said "45× too slow"

The performance budget for the generator was ambitious on purpose: at
least 100,000 messages per second, so it could never be the bottleneck
when stress-testing everything else. Budgets exist to be *validated, then
enforced* — a budget without a measurement is a wish.

The first honest measurement: **2,200 messages per second.** Not 10%
short. Forty-five times short. This is the moment where the project's
entire ethos gets tested, because the tempting response is to quietly
tune something, re-run, and hope. Instead: profile it, and find out
*where* the time actually goes before touching anything.

## The profiler versus my intuition

My intuition was confident: it's the JSON serialization. Each message
gets validated and serialized through a schema model; surely that's the
cost. I would have spent an evening swapping serializers.

The profiler said my intuition was worthless. Serialization was about two
microseconds per message — it runs in compiled Rust under the hood and
was already cheap. **Ninety-three percent of the runtime was in one
place: dangling-edge repair on delete.** When a resource is deleted, any
resource pointing at it needs its edge fixed so the world stays
consistent. My naive implementation scanned *every* resource in the world
on *every* delete to find the dependents — roughly 88 million comparisons
across the benchmark. The cost wasn't in the obvious, glamorous place; it
was in an innocuous-looking helper I hadn't given a second thought.

Two fixes, both structural rather than clever:

1. A **reverse-dependency index** (target → the resources pointing at it),
   so repair touches only the dependents instead of the whole
   world. This alone took the kernel from 2,200 to roughly 80,000
   messages per second.
2. A second index so picking a hot-set target no longer rebuilt a list on
   every message.

Result: **~88,000 messages per second** in the generation kernel. Had I
followed my intuition and optimized serialization, I'd have made the one
fast part slightly faster and moved the needle by nothing. That's the
whole argument for measuring: not that profiling is virtuous, but that
human intuition about performance is routinely, confidently wrong.

## The number that kept moving, and why

End-to-end through the command-line tool, sustained throughput came in
lower — around 36,000 messages per second — and, tellingly, it *degraded*
across a long run: 39k, then 36k, then 32k. That's not noise; it's a
signal, and it's worth explaining rather than reporting the best number
and moving on.

The cause is that the world *grows* during a run. Creates happen more
often than deletes (5% versus 3%), so a two-million-message run takes the
world from ten thousand resources to about fifty thousand, and
per-message index costs drift upward as the structures get bigger — made
worse by thermal and memory pressure on an 8GB laptop also running the
store. Naming that mechanism is more valuable than the headline number,
because it tells a future reader exactly which assumption will break at a
larger scale.

## Knowing when to stop — on principle, not exhaustion

I could have closed the remaining gap to 100k by disabling message
validation in the hot path. I chose not to, and recorded why: a generator
that *provably emits valid messages* is a feature, not overhead. The
validation is what lets every downstream phase trust its input
unconditionally. Trading that for a bigger number would be optimizing the
benchmark at the expense of the thing the benchmark is supposed to
protect.

So the budget itself moved — the right way. The 100k target was **amended
by supersession**: retired to ~30k sustained end-to-end on laptop
hardware, with the reasons recorded (pure Python with validation
deliberately kept on; world growth during long runs). The two algorithmic
bottlenecks I found are fixed and documented. The original figure isn't
edited away as if it never existed; it's superseded, with a paper trail.
A performance budget is a falsifiable claim, and this is what it looks
like when the claim gets falsified honestly.

## What I'd take to the next project

- **Build your ground truth first.** A deterministic generator isn't test
  scaffolding you bolt on later; it's the instrument that makes every
  subsequent measurement possible. Reproducibility is what turns "it felt
  faster" into a number.
- **Profile before you optimize — always.** My intuition pointed at
  serialization; the truth was an O(n) scan hiding in a helper. The
  ninety-three percent was invisible until measured, and would have
  stayed invisible if I'd trusted my gut.
- **Let budgets be falsifiable.** Missing a target by 45× is a *finding*,
  not a failure — as long as you profile it, fix what's fixable, and
  amend the budget with the reasons on the record rather than quietly
  lowering the bar.

The generator now feeds the next phase: the graph hot store, where the
same measure-don't-assume discipline produced an even better story — a
benchmark that first told me the graph database was 40× *slower* than
plain SQL, until the flatness of the numbers revealed the result was a
bug in my own query. That's the next post.
