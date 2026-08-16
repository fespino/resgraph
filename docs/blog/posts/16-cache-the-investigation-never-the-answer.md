---
date: 2026-08-16
categories:
  - AI agents
tags:
  - caching
  - serving
  - evals
  - determinism
  - observability
---

# Cache the investigation, never the answer

The gateway from the last post has two cache layers, and almost
everything that went wrong or almost-wrong in building them traces to
one root: they solve different problems, and cache intuitions from
one leak into the other. The provider's prefix cache saves work on a
*shared prefix across different requests* — the analyst's system
prompt and tool schemas, identical on every call. The gateway's
response cache saves work on *identical full requests* — the same
bytes in, the stored response out. Separate mechanisms, separate
failure modes, separate metrics. Conflating them is the most common
cache error, so the decision record names them apart and this post
keeps them apart.

The deeper finding is about caching an *agent*, and it is the title:
for an investigating agent, you can cache the steps of the
investigation, but the answer must always be earned against the live
world. Getting there took three review questions, each of which
sharpened the design — and the last of which produced the strongest
invariant in the phase.

## The prefix cache: the gateway's job is to not break it

The provider-side prefix cache predates the gateway; the platform's
prompt architecture was cache-aware from birth (stable prefix, tools
before variable content). The gateway's obligation is purely
negative — route without reordering or rewriting messages, pass the
per-request cache-usage fields through — because a gateway that
busts its upstream's cache is a net negative at any hit rate.

Writing the *proof* of that obligation found two holes that had
survived every existing test. The gateway's request model rejected
the analyst's real system shape — a block list carrying
`cache_control` — so the caching discipline literally could not pass
through the hop. And the response model dropped the provider's cache
usage fields, which means the hit-rate instrumentation would have
read zero through the gateway forever, indistinguishable from a dead
cache. Two testing lessons rode along: *incidental coverage is not
regression coverage* — the one guard that existed was there because
a fixture happened to use blocks, and a named test now states the
claim; and *a fake smart enough to be wrong needs tests of its own*
— a memoizing cache fake got cut for a dumb one whose assertions
carry the proof.

The offline proof then got its one paid receipt, with the
pre-mortem treatment any paid run gets here. Attempt 1 failed
through the pre-mortem's own registered failure mode, and the
registered guard (cache-creation tokens must be nonzero before
reading anything into call 2) stopped any false conclusion: the
pilot's prompt — the analyst's system prefix alone, 3,611 tokens —
sits *under* the provider's 4,096-token cacheable minimum, so the
provider cached nothing, for $0.007. The premise was wrong in an
instructive way: the real analyst caches fine because its tool
schemas serialize into the prefix and carry it over the minimum.
The prefix you cache is bigger than the prompt you wrote. Attempt 2,
at the real analyst shape (~4.4k tokens): creation 4,800 on call 1,
read 4,800 on call 2, through a running gateway hop, with the
gateway's own response cache provably out of the way. Three cents,
total. The division of labor is the honest one: fakes prove the
gateway can never regress its half; one paid pair proves the
provider's half, once.

## The response cache: eligibility is data, hits confess

The second layer is a full-request hash — alias plus canonical
request kwargs — mapped to a completed response, 15-minute TTL,
LRU-bounded. Three rules keep it honest:

- **Eligibility is data, and narrow.** Only non-streamed requests
  whose serving setup declares `temperature: 0` are cacheable — the
  deterministic local workers qualify; the sampled paid setups never
  do, because replaying one random draw as *the* answer would pass
  off a sample as a truth. Streams are never cached.
- **A hit says so.** Every served-from-cache response carries
  `cached: true`, with usage preserved from the original generation.
  A hit indistinguishable from a generation would be a quiet lie.
- **Identity is the full request semantics.** A one-token difference
  is a different resource — precisely where HTTP's URL-plus-Vary
  intuition breaks.

## Three questions, escalating

The design earned its shape under review interrogation, and the
sequence is worth keeping because each question found something its
premise didn't predict.

**"Couldn't this serve stale results?"** Classic staleness turns out
to be structurally absent — and the reason is load-bearing. The
model has no side channel to the data stores: the world state the
analyst reasons over lives *in the request bytes*, fetched
harness-side and delivered as tool results. A changed world is a
changed byte sequence is a different key is a miss. But the question
flushed out the real hazard, which is not staleness: **the cache is
an instrument hazard.** This platform has three measurement paths a
response cache would silently corrupt — the eval suite (k identical
trials served from cache replay one draw, and pass^k collapses into
pass@1), the load test (byte-identical replayed traffic absorbed by
the cache idles the backends, and the measured knee is the cache's,
not the model server's), and the failover drill (cached replays mask
the killed backend, reporting resilience the serving path never
demonstrated). All three are the same failure shape a drill
postmortem here once named — a run that completes, produces numbers,
and measures nothing — delivered by an optimization instead of a
bug. Hence `cache_responses: false` on every measured call, honored
on read *and* write, stated at design time rather than discovered at
drill time. A serving optimization must be audited against every
measurement path, not just the traffic.

**"Do we always send the entire world state?"** No — and precision
here matters: the request carries the *observed slice*, not the
world. What the agent fetched this turn is in the tool-result bytes;
what it never observed is invisible to model and cache alike. That
forced the design documentation to show real analyst-shaped traffic
— a tool-use turn, the tool result carrying the fetched resource
with its config version and `fetched_at` — instead of toy prompts.

**"What if the world changes under the same question?"** This
produced the convergence invariant. The question alone never
produces the answer — the analyst investigates. A pre-tool turn can
only cache the model's decision of *what to look at*, which a
deterministic model emits identically for identical bytes anyway.
The tools then execute live against the current stores — tool
execution is never cached by this layer — and a changed world enters
the next turn's bytes: different key, miss, fresh reasoning from
that point on. For an entire run to be served from cache, every tool
result must be byte-identical to a prior run's — that is, the world,
as observable through the tools, did not change. And since every
tool envelope carries `fetched_at`, even that is rare outside
deliberate replays: cross-run hits effectively occur on pre-tool
turns and in the replay traffic the layer exists for. The answer is
never cached; the investigation's steps are.

The honest residue, stated rather than hidden: a world change
*invisible to the tools* can make a cached answer wrong — and it
makes the fresh answer identically wrong. That defect belongs to the
tool layer's sight, not to cache policy. The remaining window is a
model swapped under the same alias inside one TTL.

## The replay receipt: the path that had never run

The phase's exit gate demanded a measured hit-rate table on replayed
real traffic — and collecting it delivered the phase's cheapest
lesson. The eval-suite-through-gateway wiring, the exact path the
headline claim rested on, had *never executed*: the paid pilot had
gone through the seam client directly. "The suite is wired to the
gateway" was a claim about a code path, and a code path that has
never run is a hypothesis.

The $0 rehearsal — one recorded item replayed twice with the
response cache on — crashed twice before it measured anything. The
eval CLI assumed every setup carries a `model` key (gateway setups
don't; the gateway resolves serving). Multi-turn conversations died
serializing echoed tool-use blocks back onto the wire. Both got
fixes with regression tests, and both are exactly what the rehearsal
was for: the crashes were the receipts. Then the numbers came in
better than registered — the replay hit 4 of 4 cacheable lookups
covering the whole recorded four-turn investigation byte-for-byte,
serving 432.6 seconds of recorded local generation in 0.07 seconds,
13,634 backend tokens unspent, while sampled traffic stayed
uncacheable by design. The paid receipt then passed first try
(~$0.12): a normal suite row whose new `llm_trail` field records the
winning source, backend, and cache outcome for every call — per-call
*outcome* joining the routing *intent* the rows already carried.

## What breaks at 1000×

Both layers change character with scale, in different directions.
The response cache's honesty rules are the fragile ones: at
multi-tenant scale, "identical bytes" can span users, and a shared
full-request cache becomes a cross-tenant information channel unless
identity joins the key — the single-user design gets to ignore what
a fleet cannot. The measured-run bypass also stops being a flag a
disciplined client remembers: with hundreds of eval and drill
consumers, run context has to propagate structurally (the way trace
context does) so that *being a measurement* is a property the
platform enforces, not a convention each script honors. The
convergence invariant itself scales cleanly — it depends only on
"the model has no side channel to the stores," which is an
architecture property worth defending at any size. And the prefix
cache flips from optimization to economics: at fleet volume the
prefix-cache hit rate is a line item that dwarfs the response
cache's savings, which means the gateway's purely negative
obligation — don't break the upstream cache — becomes a tested
invariant on every routing change, exactly as it is here, just with
more zeros on the bill it protects.

The decision record is D32 in
[SPEC.md](https://github.com/fespino/resgraph/blob/main/SPEC.md).
The prefix-cache proof is
[PR #210](https://github.com/fespino/resgraph/pull/210) and its paid
receipt [PR #214](https://github.com/fespino/resgraph/pull/214); the
response cache — with the full worked example, the world-change
counterexample, and the convergence argument — is
[PR #211](https://github.com/fespino/resgraph/pull/211); the replay
table and the suite-through-gateway receipt are
[PR #221](https://github.com/fespino/resgraph/pull/221).
