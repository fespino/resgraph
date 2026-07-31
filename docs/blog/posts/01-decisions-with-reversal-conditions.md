---
date: 2026-07-27
categories:
  - Foundations
tags:
  - spec
  - design
  - testing
---

# Decisions with reversal conditions: a spec that fights back

Six months into any side project, you hit a line of code you don't
understand and can't remember writing. Why is this field a string and
not an enum? Why does the delete path behave differently? You made a
decision, it mattered, and the reasoning evaporated. The knowledge that
would let you *safely change* the code is gone, so you either cargo-cult
around it or break something.

The fix isn't more comments. It's treating decisions as first-class
artifacts with a specific shape: a recorded rejection and a reversal
condition. **A decision without a recorded rejection is just an
opinion** — you can't tell later whether you weighed the alternative or
never saw it.

<!-- more -->

This is the second post about **resgraph**, a mini referential data
platform built in public. This one is still phase zero — the
specification that every later phase cites by number. If the security
post was about the controls the repo runs *on itself*, this is about the
document that keeps the repo *honest with itself* as it grows. Every
future component — the ingest, the query layer, the agents — will point
back at a decision here, so the specification is the load-bearing wall
the rest of the house hangs off.

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-0-foundations`](https://github.com/fespino/resgraph/tree/phase-0-foundations).

## The shape of a decision

Every locked decision in the spec carries an ID (`D1`, `D2`, …) and three
things:

1. **The decision**, stated plainly.
2. **The rejected alternative**, with *why* it lost.
3. **The reversal condition** — the specific evidence that would make us
   change our mind.

Changing a locked decision isn't an edit; it's a *new* decision that
supersedes the old one. The history stays intact. This sounds
bureaucratic for a solo project, but it's the opposite: it's what lets me
move fast without fear, because I can always reconstruct why the ground
is shaped the way it is.

Here's a real one, compressed. **D1: use Memgraph for the graph store.**
Rejected: Neo4j — better name recognition, but a heavier local footprint
for a laptop-scale project. Reversal condition: if we hit tooling gaps
that cost more than a day, or if traversal benchmarks disqualify it,
switch — the Bolt driver and Cypher queries carry over, so only the
compose file and index DDL change. That last clause matters: it says how
*expensive* reversal would be, which is half of whether a decision is
safe to make now.

## The decision that had to be executable

The most important decision in phase zero is **D2: the update-message
schema** — the contract between the generator that produces events and
the ingest that consumes them. Get this wrong and every downstream
component inherits the mistake.

The subtle risk with a schema-in-a-spec is *drift*. You write the schema
in a design doc, you implement it in code, and over months the two
quietly diverge until the documentation is actively lying. I wanted that
to be impossible, not merely discouraged.

So the specification's example message is written in a fenced JSON block,
and a test **parses that exact block out of the spec file** and validates
it against the code's schema model. The spec's example *is* a test
fixture. If the schema code and the spec's example ever disagree, the
build goes red. That one block of documentation cannot drift, because
its drift is a failing test — the prose around it can still rot like
any prose, but the contract itself can't.

This did its job on the very first run — and not in the way I expected.
The test failed immediately, but not because the schema was wrong:
because I'd written the spec's fence as a bare ``` instead of ```json`,
so the test couldn't *find* the block to parse. The anti-drift mechanism
caught a defect in the anti-drift setup itself, on day one. That's the
signal that the test is real: it fails when reality disagrees with your
intent, including when your intent is sloppily expressed.

## Enforce the semantics, don't just describe them

The schema decision has a second lesson. It's not enough to say in prose
"a delete message carries no attributes" — prose doesn't run. So the
model *enforces* the normative rules the spec states:

- A `delete` operation with a non-empty payload is **rejected**, not
  quietly accepted.
- Timestamps must be timezone-aware — a naive datetime is a validation
  error, because ambiguous time is the seed of a whole class of
  downstream bugs.
- Parsing is **strict**: an unknown field is a producer bug, not
  forward-compatibility, so it's refused.
- Messages are immutable once constructed.

The distinction I kept coming back to: the guide I was loosely following
*documented* these semantics; the code *enforces* them. Day one, I
disagreed with "documented" and moved them into the validator, and
recorded that I'd done so. Recording the disagreement is the discipline
visibly working — a later reader sees not just what the code does but
that the choice was deliberate.

One small decision that looks wrong until you see it: the schema version
is typed as `Literal[1]`, a single allowed value, not a general integer.
That feels needlessly rigid until you realize it's the *versioning hook*.
When a version 2 arrives, it's a new model with `Literal[2]`, and a
discriminated union routes messages to the right one — the schema does
the dispatch. A plain `int` wouldn't fail as loudly as you'd hope:
strict parsing already rejects a v2 that *adds* fields, so the Literal
guards the subtler case — a version that changes what fields *mean*
without changing the shape, which is exactly the kind of message that
parses cleanly under v1 assumptions and corrupts quietly. The tempting
"single source of truth" constant for the version number was actually a
*false* one (you can't put a constant inside a `Literal`), so I dropped
it.

## Leaving decisions unmade, on purpose

Two questions came up that I genuinely couldn't answer well yet: should
duplicate relationships in a message be rejected, and should a resource
be allowed to reference itself? Rather than guess and bury the guess in
code, I parked them as a visible TODO at the top of the module — a
*decision to not decide yet*, on the record. When the ingest phase gives
me evidence about what those cases actually do to the store, I'll decide
with data instead of vibes. An honest "unknown, revisit when X" beats a
confident wrong answer that nobody remembers making.

## What I'd take to the next project

- **A decision log with rejections and reversal conditions** costs a few
  minutes per decision and saves you the archaeology later. The reversal
  condition is the underrated half: it tells future-you what evidence
  would justify a change, so revisiting isn't relitigating.
- **Make the spec executable where you can.** A schema example that's
  also a test fixture cannot rot. The cheapest documentation is the kind
  that fails the build when it lies.
- **Enforce normative rules in code, not prose.** If the spec says
  "must," something should reject the "must not."

Next post is where the data-driven part really begins: the deterministic
world generator — and the benchmark that told me my first implementation
was 45 times too slow, plus the profiler result that proved my intuition
about *why* was completely wrong.
