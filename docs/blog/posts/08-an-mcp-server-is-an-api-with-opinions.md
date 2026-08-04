---
date: 2026-08-04
categories:
  - AI agents
tags:
  - mcp
  - tool-design
  - api-design
  - skills
  - budgets
  - benchmarks
---

# An MCP server is an API with opinions

The most load-bearing section of this platform's new MCP server card
is titled "What this server will NOT do." No writes of any kind. No
unbounded queries — depth caps, token caps, and pagination enforced
server-side, not requested politely. No authority from the caller —
nothing in any tool schema lets a model claim its own permissions.
That section is the thesis of this phase: an MCP server is not a
transport bolted onto an API, it is an API with opinions about its
consumer — a consumer that reads error messages as instructions,
pastes every response into a finite context window, and will
eventually be trusted with an incident. Design for that consumer and
the tool surface changes shape.

<!-- more -->

This is the ninth post about **resgraph**, a mini referential data
platform built in public. Part I built the pipeline: a deterministic
generator streams infrastructure updates, a consumer applies them
idempotently into a graph store, a cold store keeps full history in
Iceberg, one query layer answers questions needing both, and an
observability layer watches it all — closed by a chaos drill with a
public incident report. Part II begins here, and it is the reason
the platform exists: making these capabilities into tools an agent
can be trusted with. This phase ships the MCP server (D19 in the
spec), server-side budgets (D20), and investigation playbooks as
MCP prompts (D21).

!!! info "The repo at this phase"
    Browse the repository exactly as it stood when this was written:
    [`phase-7-mcp-server`](https://github.com/fespino/resgraph/tree/phase-7-mcp-server).
    The server card — the contract this post walks through — is
    [`CARD.md`](https://github.com/fespino/resgraph/blob/phase-7-mcp-server/src/resgraph/mcp/CARD.md).

## One registry, two surfaces, zero drift

Every tool is a plain function with a Pydantic input model, a
Pydantic output model, and one thing the LLM never sees: a
keyword-only caller context, injected by the transport and absent
from the LLM-facing schema — so a caller cannot supply its own
authority, structurally. One `TOOL_REGISTRY` declares the surface,
and both consumers derive from it in a loop: the MCP server
registers its tools from the registry, and the HTTP API mounts
`/tools/{name}` routes from the same entries. The transport is
never the truth-bearing module.

"Derive from one source" is a rule that decays unless something
enforces it, so the phase ships a drift guard: CI assertions that
parse the source tree as an AST and fail on any tool registration
outside the registry loop, any `/tools` route outside the mount,
and any model class duplicated across surfaces — plus a signature
check that every registered implementation matches its declared
models exactly. Not "currently no drift" — *structurally cannot
drift without failing the build*. The
same idea as a type system: make the wrong state unrepresentable,
then stop reviewing for it.

## Task-shaped, not route-shaped

The five tools are the questions an investigator actually asks, not
the REST routes the platform happens to have: `blast_radius` (what
breaks if this dies — live or as of time T), `dependency_path` (why
does A depend on B), `resource_history`, `world_diff`, and one
polymorphic `fetch_resource`. An agent investigating an incident
wants `blast_radius(db-42)`, not five REST calls it must
orchestrate itself. Anthropic's
[Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
calls the discipline agent-computer-interface design, and the
principle is poka-yoke: make the mistake structurally hard, not
instructed-against.

The interesting part is the alternative that *almost* wins.
Microsoft's Azure SRE team
[reported](https://techcommunity.microsoft.com/blog/appsonazureblog/context-engineering-lessons-from-building-azure-sre-agent/4481200/)
collapsing 100+ narrow tools into roughly five *wide* ones — whole
CLI ecosystems like `az` and `kubectl` handed to the model as
single tools. That endorses the small count while disputing the
shape, and their winning argument — lean on the model's
training-data knowledge of those CLIs — simply does not transfer to
a bespoke surface no model has ever seen. So task-shaped stands,
but the alternative is recorded in the spec *with a trigger*: if
agent traces show improvisation around the surface — questions the
five tools cannot compose — a wide/files-based arm gets tested
before tool six gets added. Same treatment for composition-in-code:
Anthropic's
[code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
keeps intermediate results in an execution environment instead of
the context window (their benchmark: 150k → 2k tokens, a 98.7%
reduction), and the trigger is written down — when traces show rote
multi-tool sequences or in-context set operations over ref lists,
composition moves server-side. A rejected alternative with a named
trigger is a decision; a rejected alternative without one is a
mood.

Every tool also declares its risk annotations explicitly —
read-only, non-destructive, idempotent, closed-world — because the
[MCP tool-annotation defaults](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)
read as destructive-and-open-world when unstated, and, as that post
puts it, "risk profile is a property of the session, not of any
single server": the client composing your read-only server with
someone else's write-capable one is the thing being defended.
Per-tool metadata carries `timeout_s` and a structured
error-action map (rephrase / retry / give up) — two of the
operational gaps named by an
[enterprise MCP deployment field report](https://arxiv.org/abs/2603.13417).

## Budgets are enforced, not requested

The agent must not be able to ask an unbounded question — and
enforcement lives server-side, because prompt etiquette is not a
control:

- **Clamps, not errors.** A `depth` beyond the traversal cap clamps
  and says so (`depth_clamped: true`). An agent that gets clamped
  keeps working; one that gets a 500 retries in a loop.
- **Refs + fetch.** Traversals and diffs return bare refs — id,
  type, one line — and `fetch_resource` returns detail for the few
  that matter. A 400-node blast radius with full attributes is a
  blown context window the agent cannot steer. Azure SRE's report
  lands the same rule from production: treat large tool outputs as
  data sources, not context.
- **A hard token cap on every response.** Overflow paginates with
  `truncated: true`, an honest `total_count`, and the next move
  written in prose — `pagination_hint: "call again with offset=N"`
  — because the consumer is a language model, so the payload itself
  teaches the follow-up. Pagination is an argument, not a separate
  tool; a truncated radius is "at least N", never "N".
- **Freshness on everything.** Every response carries `fetched_at`
  and its source store. An agent reasoning over a twenty-minute-old
  payload about a live system needs to know to re-fetch; freshness
  *is* correctness when the world churns.
- **Errors are steering surfaces.** A rejected filter answers with
  the correction in the error message, not in documentation.
  [Stripe's steering experiments](https://stripe.dev/blog/ai-steering-experiments)
  measured the asymmetry that justifies this: passive documentation
  goes unread — "agents simply don't wander" — while error-based
  steering reliably corrects behavior at exactly the moment the
  agent is paying attention.

## The benchmark that keeps the shape honest

Per this series' standing rule, a design claim about payload size
needs a number, a method, and a hardware label. The payload
benchmark measures the canonical refs-and-cap response against the
same traversal serialized fat, across 1k/10k/100k-resource worlds —
and the honest headline is that **at natural radii the cap barely
matters**: seed-42 blast radii top out around 30 nodes, refs-vs-fat
is a 3–4× constant factor, and both fit any context window.

The row that justifies the design is the constructed one — a
900-dependent hub host, the shape a real cloud always contains and
random worlds rarely produce:

| Hub response (900 dependents) | tokens |
|---|---|
| refs page (`truncated: true`, `total_count: 900`) | **7,172** — under the 8,000 cap |
| fat (all 900 nodes, full attributes) | **53,775** — 6.7× the cap, linear in fan-out |

Fat is unbounded — linear in fan-out, so one hub query can eat a
quarter of a context window on its own. The refs response is capped
*by construction*, with pagination picking up the remainder. And
the companion number that makes refs+fetch work as a contract:
`fetch_resource` detail is flat at ~114 tokens p100 across every
world size — following a ref costs the same in a 1k world and a
100k one. The cap only earns its keep on hubs; the hub is what the
cap is for.

## Playbooks ship as prompts

Tools say what the agent *can* do; the phase also ships two
opinions about what it *should* do — investigation playbooks
exposed as MCP prompts: `incident-impact` ("what breaks if X
dies?") and `change-forensics` ("what changed around the time
things broke?"). Each is a markdown skill with a
Pydantic-validated manifest, and its tool references are checked
against the registry at startup: a playbook naming a tool that
doesn't exist fails loudly at boot, never silently at runtime.

The bodies follow a fixed six-section shape — Goal, When to use,
Steps, Tools to call, Examples, Anti-patterns — so a consuming
model learns the format once, and they state constraints before
narrative:
[Stripe's grounding-file work](https://stripe.dev/blog/build-stripe-salesforce-integrations-faster-with-agents)
found that a constraint-first core (rules, signatures, failure
modes — not tutorials) took an agent task from hours of failed
iterations to minutes. The count stays at two on evidence, not
minimalism for its own sake:
[LangChain's skills evaluation](https://www.langchain.com/blog/evaluating-skills)
measured 82% task completion with curated skills against 9%
without — and wrong-skill selection appearing at around twenty
similar skills, so similarity at scale, not count alone, is the
ceiling. And the exit is pre-registered: if prose playbooks plateau
under evaluation, the named next experiment is compiling them into
schema-validated step graphs, per
[AIP](https://arxiv.org/abs/2606.04781)'s measured 53% → 67% with
step-level repair.

## The pin that was a wish

The spec pinned the server to MCP revision `2026-07-28` — the
[stateless-core release candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/),
which fits this surface exactly: all five tools are single-shot
reads, no session state, no handles. Then reading the RC post
against the implementation found the embarrassing part: the SDK
negotiates that revision only on the modern stateless `discover`
path, and the phase's own protocol test was using the legacy
`initialize` handshake — which negotiates an *older* revision. The
suite was green while exercising the exact path the pinned revision
deprecates. The pin was untested prose. The fix is the same
discipline this series applies to performance numbers: the claim
became a CI assertion — the negotiated protocol version must equal
the pin — so an SDK upgrade that shifts it fails the build instead
of silently invalidating the spec. It went into the project's
corrections table even though it was caught pre-merge, because a
log that only audits the past isn't auditing.

Two more findings from treating the spec as a runnable checklist:

- **The stdout audit.** The
  [stdio binding](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
  has one unforgiving MUST: nothing on stdout that isn't a protocol
  message. Audit question: which code had ever actually *run*
  inside the stdio server process? Only the hot path — the
  cold-store tools, backed by the libraries most likely to print
  something on first initialization, had never initialized there,
  because the protocol test only called hot tools. Now all five
  tools round-trip through the real channel with a seeded cold
  catalog in the subprocess environment, where a single stray print
  corrupts the framing and fails the parse. Coverage holes hide
  where "it passes" and "it ran" diverge.
- **Coverage has a measurement shadow.** The protocol suite spawns
  the server as a subprocess, so every server-side line it
  exercises is invisible to the coverage instrument — the number
  *dropped* from 96 to 93 while testing improved. In-process
  integration twins for the same paths brought it to 97 and caught
  one piece of genuinely dead code. A coverage number can lie in
  both directions; know where your instrument cannot see.

One last opinion made it onto the server card because statelessness
has a user-visible consequence: offset pagination re-runs the query
per page — exactly the stateless shape the revision asks for —
which means pages are independent reads of a churning world, not a
snapshot. The card says so and tells the consumer what to do about
it (compare `fetched_at` across pages), with the RC's
explicit-handle pattern recorded as the upgrade path if consistent
pagination is ever needed. Honesty about consistency is a feature
of the contract, not a caveat buried in code.

## What I'd take to the next project

- **Derive every surface from one registry, and guard it with
  structure, not review.** The AST-level drift guard costs a test
  file; a second schema definition costs a silent divergence per
  refactor forever.
- **Shape tools around the consumer's questions, and record the
  rival shape with a trigger.** Wide tools and code-composition are
  real alternatives; writing down *what evidence would flip the
  decision* is what keeps the choice honest.
- **Put the budget in the response, not the prompt.** Clamps that
  explain themselves, caps with honest counts, errors that steer —
  every control this phase added works even on an agent that read
  none of the documentation, which is the only agent you should
  design for.
- **Benchmark the payload shape against the pathological case.**
  Natural test worlds flattered the fat responses; the hub row is
  the design justification. Construct the case your generator
  won't hand you.
- **A pin is prose until a real call asserts it.** The revision pin
  and the stdout MUST were both "true" and both untested; running
  the spec as a checklist found the divergence a green suite could
  not.

The surface is built and the opinions are enforced. The next three
posts put a consumer on it: an analyst agent that triages incidents
through these five tools — and the evaluation harness, built first,
that decides whether anything it says can be trusted.
