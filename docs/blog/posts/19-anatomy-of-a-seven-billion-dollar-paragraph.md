---
date: 2026-08-18
categories:
  - AI agents
tags:
  - serving
  - gateways
  - routing
  - economics
  - news
---

# Anatomy of a seven-billion-dollar paragraph

Stripe [bought OpenRouter for $7
billion](https://finance.yahoo.com/technology/ai/articles/stripe-acquires-openrouter-7b-turning-091812340.html),
and the coverage compressed the product into one paragraph:

> OpenRouter functions as a unified API gateway, providing developers
> with access to over 400 AI models through a single interface. By
> offering centralized billing and model routing — a process that
> determines which AI model handles a specific request based on cost,
> speed, and capability — the platform has scaled to 8 million global
> users.

Every clause in that paragraph is a mechanism. This platform runs a
deliberate miniature of the same layer — one gateway over six local
and remote model setups, built in the serving phase — which means the
paragraph can be taken apart clause by clause and each clause
explained by the code that implements it, with the distance to the
real thing measured rather than waved at. That is this post. Claims
about OpenRouter's mechanisms cite [their
documentation](https://openrouter.ai/docs/features/provider-routing);
claims about ours cite merged decisions.

First, the picture. Imagine a city with four hundred restaurants —
the AI models. Every family used to call each restaurant separately:
different phone numbers, menus in different languages, different ways
to pay. Then someone opens one counter in the middle of the market.
You order there, once, in one language. The counter knows every menu,
every price, who's open, who's slow today — it picks a restaurant for
you, and if that one's closed, quietly picks another. You pay the
counter; the counter pays the restaurants. The middle turns out to be
the best spot in the whole market: the counter sees every order in
the city and holds the money as it flows through. That is what Stripe
paid seven billion dollars for — not any restaurant, the counter. And
the web's original design expected counters: HTTP's layered-system
constraint explicitly allows standing in the middle, because its
designers knew that routing, caching, policy, and failover would live
there. The sale is a receipt for that architecture.

Our gateway is a tiny counter in the same spot with the same jobs:
one language, pick the restaurant, watch who's slow, count the money
— six restaurants instead of four hundred. The rest of this post is
the paragraph, clause by clause.

## Clause 1 — "access to over 400 AI models through a single interface"

A single interface is two things: a wire shape and a name catalog.

The wire shape is the part everyone gets for free by now — an
OpenAI-compatible request schema is the lingua franca, and our
gateway speaks a close dialect of it (D30 — Gateway shape: two
backends, task-class routing, recorded source). The interesting part
is the catalog. Our `model` field carries a served-model *alias*,
never a raw provider id: where an alias actually runs — provider,
base URL, real model id, request kwargs — is a property of its
registry entry, resolved at dispatch (D29c — the provider-pluggable
seam). The payoff is that adding a provider is a config row, not
code, and nothing downstream can sniff a name to guess a backend.
Their 400 models is our six setups with more rows; the seam is the
same shape.

The measured distance: OpenRouter's catalog has one primitive ours
lacks. In their model, one *model* is served by many *provider
endpoints* — the routable unit is the endpoint, and there is a
[per-model endpoints
API](https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model)
listing each provider's price, quantization, and live performance for
the same weights. Our registry conflates the two: one alias, one
serving location. Every routing mechanism in the next clause is
degenerate without that split — choosing among two fixed backends is
a coin flip; choosing among N endpoints per model is an algorithm.
That distinction is the first workstream of the gap slate.

## Clause 2 — "model routing … based on cost, speed, and capability"

The press paragraph's middle clause names a selection pipeline, and
the documented order of operations matters: capability filters,
health prioritizes, speed and cost weight.

**Capability filters.** A provider that cannot run the request —
wrong context length, no tool support — is not a candidate at any
price. OpenRouter checks this per request when the caller sets
`require_parameters`; we resolve it through task classes, where each
class of work routes to setups declared able to serve it. Theirs is
the stricter contract (the request states what it needs; the catalog
answers), and adopting per-request admission is on the slate.

**Health prioritizes — it does not filter.** The documented mechanism
is precise: providers with significant outages in the last 30 seconds
are *deprioritized*, not removed — they remain in the pool as
fallbacks. That soft form matters because binary health is a blunt
instrument: a backend at a 20% error rate should earn less traffic,
not zero. Our health machinery is the hard form — probe-driven state
with gradual readmission after three consecutive healthy probes, and
the probes are tiny generation probes rather than TCP pings, because
a server can 200 its health endpoint while generation has collapsed
(D33 — Serving SLOs + capacity honesty). We have down-and-readmit; we
lack earn-less-while-flaky. The slate adds the soft form beside the
hard one.

**Speed is a percentile set, not an average.** OpenRouter tracks
latency and throughput per endpoint as p50/p75/p90/p99 over a rolling
five-minute window, and exposes them in the endpoints API. Our
gateway keeps a single exponentially-weighted moving average of
time-to-first-token — and our own load test is the argument against
our own design: measured TTFT on this hardware is bimodal (0.5 s on a
warm prefix vs ~12 s on a cold prefill), and the mean of a bimodal
distribution describes no request that ever happened. Percentile
windows are the speed clause done properly; the EWMA was the
placeholder. This is the clearest case in the whole comparison where
the big system's design is simply right and ours is scheduled to
converge.

**Cost is a weighted lottery.** The documented default is
inverse-square price weighting: with providers at $1, $2, and $3 per
million tokens, the cheapest is nine times more likely than the
priciest to be picked first (1/1² : 1/2² : 1/3²). It is a lottery
rather than a hard sort so that cheap providers get most of the
traffic without the expensive ones starving — a starved provider is
one you learn nothing about. Our cost mechanism is a different lever
held for the same reason: cheap-by-default tiers, with paid backends
reachable through failover under a per-day spend budget. Our failover
drill priced the unbudgeted version of that walk at $1.08/hour — the
day the free backend dies, every free call silently becomes a paid
one — which is why the budget exists (D31's fall-forward cap):
availability bought with money gets a cap and a named refusal.

One design detail from their docs worth copying outright: when a
caller sets `sort` or `order`, load balancing is disabled entirely.
The market mechanism and the caller override are mutually exclusive
by construction — you get the lottery or you get your list, never a
blend that neither party can predict.

And one vocabulary their docs get exactly right: constraints split
into hard and soft by *what happens when they fail*. `max_price`
refuses the request outright if nothing fits; `preferred_max_latency`
and `preferred_min_throughput` only deprioritize endpoints that miss
the threshold. Refusing loudly versus preferring quietly is the same
split our refusal-with-reason vocabulary draws — a request that
cannot be served within its stated constraints should fail with the
constraint named, not degrade into something nobody asked for.

## Clause 3 — "centralized billing"

The clause everyone underrates, and the one the acquirer is in the
business of. Here is the structural fact: the gateway is the one
component that sees every call with its model, its token usage, its
outcome, and its caller. Whatever sits in that position is the
natural meter, and a meter in the money path is a business.

Our miniature already has the meter half. Every call through the
gateway lands in a cost distribution sliced by task class, backend,
and routing source, and per-task cost is a service-level objective,
not just a cap — because a prompt change that doubles per-task tokens
violates an objective the first day, long before any monthly cap
trips (D33's cost-per-task amendment). Refusals carry reasons, and
the reasons are load-bearing: `budget_503` means "we refuse to pay
past the cap," which must never read as "everything is down."

Billing is the meter plus two more pieces: identity and a wallet.
Identity is knowing *who* consumed — per-caller keys with per-key
ledgers. The wallet is prepaid balance decremented from the meter,
refusing when empty. And here the doc-validation pass turned up a
convergence we did not plan: OpenRouter's error vocabulary already
draws the exact line ours does. An empty balance returns [402
`payment_required`](https://openrouter.ai/docs/api-reference/errors);
routing that cannot satisfy the request returns 503. "You spent your
money" and "we cannot serve you" are different sentences with
different audiences, and both platforms refuse to conflate them.
Their key model also splits capabilities — the key that spends is not
the key that administers (the credits API requires a management key)
— which is the shape our per-caller identity work adopts.

The remaining distance is the wallet itself and the usage surface,
and both are on the slate. Neither is conceptually hard; the point of
building them is that "centralized billing" stops being a phrase and
becomes three verifiable mechanisms with tests.

## What the anatomy is for

Two design debates surface once the paragraph is in pieces, and they
are worth naming because they are decisions, not features.

**Who is sovereign over routing?** The caller knows its request; the
operator knows the fleet. Every mechanism above picks a side:
caller-set constraints narrow the candidate set but never broaden it
past what the registry routes — the registry stays the authority, and
a caller can only shrink its world. OpenRouter's
sort-disables-balancing rule is the same principle stated
differently: override and market mechanism never blend.

**Compliance is a routing dimension.** Their provider preferences
include `zdr` (zero-data-retention-only routing) and `data_collection`
— where your prompt may go is a constraint of the same kind as how
much you will pay. A gateway that can route by price can route by
policy with the same machinery, which is why policy belongs in the
selection pipeline rather than bolted beside it.

The gap between our counter and theirs is now a public work list —
the [gateway phase
charter](https://github.com/fespino/resgraph/issues/263) files every
distance named above as a workstream, each buildable and measurable
at laptop scale against replayed traffic for roughly nothing. Some
distances we decline on purpose: no mutable "latest" aliases (a name
that silently changes meaning breaks reproducibility — pins are this
platform's thesis), no leaderboards without a population, no
400-model catalog for its own sake. The claim was never scale. The
claim is that every clause in a seven-billion-dollar paragraph is a
mechanism you can build, measure, and state the size of — and that
doing so at six-backend scale teaches you exactly what the
acquisition was pricing at four hundred.

All numbers here are laptop-scale and labeled as such; methodology
and hardware for every measured figure live in the repository's
benchmarks log.
