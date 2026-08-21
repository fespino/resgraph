"""The gateway process: one `/v1/generate` fronting the named setups.

Requests resolve through the precedence router, admit through the bounded
per-backend queues, and are served by the same clients the eval seam builds —
the setup's provider decides the backend, never the alias's spelling. The
winning source, the serving backend, and any fallback chain ride every
response.

Failure semantics: a pin fails loudly and never substitutes; other traffic
walks to the next eligible backend on any failure before the first token,
each hop logged with a `[gateway:fallback]` tag and the walk alerted once
the chain exceeds one hop; an exhausted walk is a clean 503 tagged
`[gateway:exhausted]`. Streamed requests share the same walk for opening;
once tokens flow, the relay's rules apply — a zero-token death restarts
silently (it is an init failure), a death after tokens surfaces a
structured `stream_error` and never resumes elsewhere. Streaming is served
where a stream adapter exists (the chat-completions backends); an
anthropic-setup stream answers 501 until its adapter lands.

Decisions: D30–D33, D40–D43 (SPEC.md).
"""

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from resgraph import obs
from resgraph.evals.pricing import PRICES_PER_MTOK, estimate_cost
from resgraph.evals.providers import build_client
from resgraph.gateway.accounting import StreamAccount
from resgraph.gateway.accounts import (
    ACCOUNTS_PATH,
    UsageLedger,
    Wallet,
    load_accounts,
    mgmt_authorized,
    resolve_account,
)
from resgraph.gateway.budget import FallForwardBudget
from resgraph.gateway.cache import ResponseCache, cache_key
from resgraph.gateway.dispatch import Backend, ProbeResult, QueueFull, choose
from resgraph.gateway.quality import (
    QUALITY_PATH,
    eligible,
    fabricating,
    frontier,
    load_quality,
    stale,
)
from resgraph.gateway.registry import (
    capability_mismatch,
    catalog_rows,
    endpoint_price,
    expand,
    lifecycle_state,
    load_policy,
    policy_allows,
    validate_lifecycle,
)
from resgraph.gateway.relay import StreamEvent, StreamFactory, parse_chat_sse, relay
from resgraph.gateway.router import (
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT_MODEL,
    ClassRoute,
    RouteDecision,
    Source,
    TaskClass,
    resolve,
)
from resgraph.gateway.screen import screen

log = logging.getLogger("resgraph.gateway")

# beyond this, a quality score describes an arm that may no longer exist
QUALITY_MAX_AGE_DAYS = 90

MODELS_PATH = Path("evals/models.yaml")
POLICY_PATH = Path("evals/gateway-policy.yaml")
PROVIDER_LIMITS: Mapping[str, tuple[int, int]] = {"anthropic": (8, 16)}
DEFAULT_LIMITS = (1, 4)
PROBE_SLOW_S = 5.0
PROBE_PROMPT = [{"role": "user", "content": "Reply with the single word: pong"}]
DEFAULT_PROBE_INTERVAL_S = 60.0
UNSTREAMABLE_PROVIDERS = frozenset({"anthropic"})


class GenerateIn(BaseModel):
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    max_tokens: int = 1024
    task_class: TaskClass | None = None
    model: str | None = None
    pin: str | None = None
    stream: bool = False
    cache_responses: bool = True
    # hard constraints refuse loudly; soft preferences deprioritize
    caller: str | None = None  # attribution-only until W4 binds it to a key
    max_price: float | None = None  # hard: effective per-mtok ceiling
    preferred_max_latency: float | None = None  # soft: TTFT p50 seconds
    preferred_min_throughput: float | None = None  # soft: tps p50
    only: list[str] | None = None
    ignore: list[str] | None = None
    sort: Literal["price", "latency", "throughput"] | None = None


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class GenerateOut(BaseModel):
    content: list[dict[str, Any]]
    model: str
    source: Source
    backend: str
    fallback_chain: list[str]
    latency_s: float
    usage: UsageOut
    cost_usd: float = 0.0
    cached: bool = False


@dataclass
class Gateway:
    """The endpoint table, their clients, and one dispatch state per backend.

    ``setups`` maps endpoint id → concrete setup (id == alias for the 1:1
    case); ``aliases`` maps alias → its endpoint ids. The alias is the
    request vocabulary, the endpoint is the routable unit."""

    setups: dict[str, dict[str, Any]]
    client_factory: Callable[[dict[str, Any]], Any]
    registry: Mapping[TaskClass, ClassRoute] = field(default_factory=lambda: DEFAULT_REGISTRY)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    clients: dict[str, Any] = field(default_factory=dict)
    backends: dict[str, Backend] = field(default_factory=dict)
    cache: ResponseCache = field(default_factory=ResponseCache)
    fallback_budget: FallForwardBudget | None = None
    last_probe: dict[str, float] = field(default_factory=dict)
    # routing lottery, not cryptography; injectable for deterministic tests
    rng: random.Random = field(default_factory=random.Random)  # nosec B311
    policy: dict[str, list[str]] = field(default_factory=dict)
    quality: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    wallet: Wallet = field(default_factory=Wallet)
    usage_ledger: UsageLedger = field(default_factory=UsageLedger)

    def client(self, alias: str) -> Any:
        if alias not in self.clients:
            self.clients[alias] = self.client_factory({"name": alias, **self.setups[alias]})
        return self.clients[alias]

    def backend(self, alias: str) -> Backend:
        provider = self.setups[alias].get("provider", "default")
        # an expanded endpoint gets its own dispatch state (two serving
        # locations must not share one health/queue); implicit endpoints
        # keep provider-keyed state, the 1:1 world unchanged
        key = alias if "@" in alias else provider
        if key not in self.backends:
            concurrency, queue_max = PROVIDER_LIMITS.get(provider, DEFAULT_LIMITS)
            self.backends[key] = Backend(key, concurrency, queue_max)
        return self.backends[key]

    def candidates(self, alias: str) -> list[str]:
        """The endpoint ids serving an alias (an endpoint id names itself)."""
        ids = self.aliases.get(alias)
        if ids is not None:
            return list(ids)
        return [alias] if alias in self.setups else []

    def serving_endpoint(self, alias: str) -> str | None:
        """The endpoint that would serve this alias now: healthy before
        degraded, then lowest observed TTFT; a fully-down alias still names
        its first endpoint (the walk decides what down means)."""
        ids = self.candidates(alias)
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]  # nothing to choose; keep dispatch state lazy
        nxt = choose([self.backend(e) for e in ids])
        if nxt is None:
            return ids[0]
        return next(e for e in ids if self.backend(e).name == nxt.name)

    def routed(self) -> dict[str, str]:
        """provider → serving endpoint id, from the registry plus the global
        default. The registry is the gateway's serving authority; a setup it
        does not route is a catalog entry (an eval arm), never a backend to
        serve, walk to, or probe — an unrouted provider must not be spent on."""
        out: dict[str, str] = {}
        for route in [*self.registry.values(), GLOBAL_DEFAULT_MODEL]:
            eid = self.serving_endpoint(route.model)
            if eid is not None:
                out.setdefault(self.setups[eid].get("provider", "default"), eid)
        return out

    def alias_for_backend(self, provider: str) -> str | None:
        return self.routed().get(provider)


def _wire(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    kind = getattr(block, "type", "text")
    if kind == "tool_use":
        return {"type": kind, "id": block.id, "name": block.name, "input": block.input}
    if kind == "thinking":
        # a thinking block carries .thinking, not .text — flattening it to
        # text would read as elided reasoning in the audit downstream
        return {"type": kind, "thinking": getattr(block, "thinking", "") or ""}
    return {"type": kind, "text": getattr(block, "text", "")}


def _cost_of(gw: Gateway, alias: str, usage: UsageOut) -> float:
    return estimate_cost(
        {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": usage.cache_read_tokens,
            "cache_creation": usage.cache_creation_tokens,
        },
        gw.setups[alias]["model"],
    )


def _observe_served(
    gw: Gateway, alias: str, out: GenerateOut, task_class: str, outcome: str
) -> None:
    labels = {"backend": out.backend, "source": out.source, "task_class": task_class}
    obs.GATEWAY_REQUESTS.add(1, {**labels, "outcome": outcome})
    obs.GATEWAY_FALLBACK_CHAIN.record(len(out.fallback_chain))
    if outcome == "ok":
        cached_label = "true" if out.usage.cache_read_tokens > 0 else "false"
        obs.GATEWAY_TTFT.record(out.latency_s, {"backend": out.backend, "cached": cached_label})
        obs.GATEWAY_COST.record(_cost_of(gw, alias, out.usage), labels)
        if out.usage.cache_read_tokens > 0:
            obs.GATEWAY_CACHE_HITS.add(1, {"layer": "provider"})
        elif gw.setups[alias].get("provider") == "anthropic":
            obs.GATEWAY_CACHE_MISSES.add(1, {"layer": "provider"})


def _request_kwargs(gw: Gateway, alias: str, req: GenerateIn) -> dict[str, Any]:
    setup = gw.setups[alias]
    kwargs: dict[str, Any] = {
        "model": setup["model"],
        "max_tokens": req.max_tokens,
        "messages": req.messages,
    }
    if req.system is not None:
        kwargs["system"] = req.system
    if req.tools is not None:
        kwargs["tools"] = req.tools
    if setup.get("provider") == "anthropic":
        kwargs.update(setup.get("extra_args") or {})
    return kwargs


def _call(gw: Gateway, alias: str, req: GenerateIn) -> tuple[list[dict[str, Any]], UsageOut, float]:
    kwargs = _request_kwargs(gw, alias, req)
    backend = gw.backend(alias)
    backend.admit()
    account = StreamAccount(backend=backend.name, started_at=time.monotonic())
    try:
        resp = gw.client(alias).messages.create(**kwargs)
        now = time.monotonic()
        out = int(getattr(resp.usage, "output_tokens", 0) or 0)
        account.content(at=now, tokens=out)
        account.finish(at=now, reported_output_tokens=out)
        backend.errors.observe(True)
        if account.ttft is not None:
            backend.ttft.observe(account.ttft)
        # tps is a stream measurement (relay): one non-streamed timestamp
        # has no emission window to rate
        usage = UsageOut(
            input_tokens=int(getattr(resp.usage, "input_tokens", 0) or 0),
            output_tokens=out,
            cache_read_tokens=int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(resp.usage, "cache_creation_input_tokens", 0) or 0),
        )
        return [_wire(b) for b in resp.content], usage, account.ttft or 0.0
    finally:
        backend.release()


def _paid(gw: Gateway, alias: str) -> bool:
    return gw.setups[alias].get("model") in PRICES_PER_MTOK


def _next_alias(gw: Gateway, chain: list[str], *, ignore_budget: bool = False) -> str | None:
    """The next hop of the walk: an untried, not-down backend's serving
    alias, chosen by health then latency; None when the walk is exhausted.
    Candidates come from the routed providers only — the registry is the
    serving authority, and an unrouted catalog setup is never a hop. A
    paid candidate is eligible only while the fall-forward budget allows
    (`ignore_budget` answers "was the exhaustion budget-caused?")."""
    tried = {c.split(":", 1)[0] for c in chain}
    budget = gw.fallback_budget
    candidates = []
    for provider, fallback in gw.routed().items():
        if provider in tried or gw.backend(fallback).name in tried:
            continue
        if not ignore_budget and budget is not None and _paid(gw, fallback) and not budget.allows():
            continue
        backend = gw.backend(fallback)
        if backend.health.state != "down":
            candidates.append(backend)
    nxt = choose(candidates)
    return gw.alias_for_backend(nxt.name) if nxt else None


def _record_hop(gw: Gateway, alias: str, exc: Exception, chain: list[str]) -> None:
    failed_backend = gw.backend(alias).name
    gw.backend(alias).errors.observe(False)
    chain.append(f"{failed_backend}:{alias}")
    log.warning(
        "[gateway:fallback] attempted=%s backend=%s error_kind=%s chain=%s",
        alias,
        failed_backend,
        type(exc).__name__,
        chain,
    )
    if len(chain) > 1:
        log.error("[gateway:degraded] fallback chain length %d: %s", len(chain), chain)


def _serve_with_walk[T](
    gw: Gateway,
    decision: Any,
    attempt: Callable[[str], T],
    task_class: str,
    first: str | None = None,
    more: list[str] | None = None,
) -> tuple[T, str, list[str]]:
    """Drive the init-failure walk for any attempt shape (a full call or a
    stream open): pins fail loudly, other traffic hops first to the routed
    alias's remaining endpoints (same model, different serving), then
    cross-model until served or exhausted."""
    chain: list[str] = []
    within: list[str] = list(more or [])
    alias: str | None = decision.model if first is None else first
    while alias is not None:
        try:
            return attempt(alias), alias, chain
        except QueueFull as exc:
            obs.GATEWAY_REQUESTS.add(
                1,
                {
                    "backend": exc.backend,
                    "outcome": "rejected_429",
                    "source": decision.source,
                    "task_class": task_class,
                },
            )
            obs.GATEWAY_FALLBACK_CHAIN.record(len(chain))
            raise HTTPException(
                429, detail=str(exc), headers={"Retry-After": str(exc.retry_after_s)}
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            if not decision.fallback_allowed:
                obs.GATEWAY_REQUESTS.add(
                    1,
                    {
                        "backend": gw.backend(alias).name,
                        "outcome": "pin_failed_502",
                        "source": decision.source,
                        "task_class": task_class,
                    },
                )
                obs.GATEWAY_FALLBACK_CHAIN.record(0)
                raise HTTPException(
                    502, detail=f"pinned {alias!r} failed on {gw.backend(alias).name}: {exc}"
                ) from exc
            _record_hop(gw, alias, exc, chain)
            alias = within.pop(0) if within else _next_alias(gw, chain)
    budget = gw.fallback_budget
    budget_blocked = (
        budget is not None
        and not budget.allows()
        and _next_alias(gw, chain, ignore_budget=True) is not None
    )
    outcome = "budget_503" if budget_blocked else "exhausted_503"
    obs.GATEWAY_REQUESTS.add(
        1,
        {
            "backend": "none",
            "outcome": outcome,
            "source": decision.source,
            "task_class": task_class,
        },
    )
    obs.GATEWAY_FALLBACK_CHAIN.record(len(chain))
    if budget_blocked and budget is not None:
        log.error(
            "[gateway:fallback-budget] refusing paid fall-forward: $%.2f of $%.2f "
            "daily cap spent; chain=%s",
            budget.spent_today(),
            budget.cap_usd,
            chain,
        )
        raise HTTPException(
            503,
            detail=f"fall-forward budget exhausted (${budget.spent_today():.2f} of "
            f"${budget.cap_usd:.2f} today); refusing to pay past the cap; chain={chain}",
        )
    log.error("[gateway:exhausted] chain=%s", chain)
    raise HTTPException(503, detail=f"no backend could serve; chain={chain}")


def _no_streamable_fallback(gw: Gateway, decision_model: str, unstreamable: frozenset[str]) -> bool:
    down_provider = gw.setups[decision_model].get("provider", "default")
    for provider, alias in gw.routed().items():
        if provider == down_provider or provider in unstreamable:
            continue
        if gw.backend(alias).health.state != "down":
            return False
    return True


def _quality_route(gw: Gateway, task_class: TaskClass | None) -> RouteDecision | None:
    """Route a task class among its measured candidates: floor first,
    then a free arm if one clears it, else an inverse-square lottery on
    measured cost per passed triage. No eligible candidate degrades to
    the class's static default — quality routing never turns a servable
    request into a refusal."""
    if task_class is None:
        return None
    route = gw.registry.get(task_class)
    if route is None or not route.candidates or route.min_passk is None:
        return None
    disqualified = fabricating(gw.quality, task_class, list(route.candidates))
    if disqualified:
        log.warning(
            "[gateway:quality] %s: %s disqualified, the measured run fabricated",
            task_class,
            sorted(disqualified),
        )
    ok = eligible(gw.quality, task_class, list(route.candidates), route.min_passk)
    if not ok:
        log.warning(
            "[gateway:quality] no candidate clears pass^k %.2f for %s; static default serves",
            route.min_passk,
            task_class,
        )
        return None
    scores = gw.quality[task_class]
    admitted = frontier(scores, ok)
    if len(admitted) < len(ok):
        log.info(
            "[gateway:quality] %s: %s excluded, worse on every measured axis",
            task_class,
            sorted(set(ok) - set(admitted)),
        )
    ok = admitted
    free = [a for a in ok if not scores[a]["cost_per_passed"]]
    if free:
        pick = max(free, key=lambda a: scores[a]["passk"])
    else:
        pool, pick = list(ok), ok[0]
        weights = [1.0 / scores[a]["cost_per_passed"] ** 2 for a in pool]
        draw = gw.rng.random() * sum(weights)
        acc = 0.0
        for a, w in zip(pool, weights, strict=True):
            acc += w
            if draw <= acc:
                pick = a
                break
    entry = scores[pick]
    return RouteDecision(
        model=pick,
        source="quality_route",
        fallback_allowed=True,
        rationale=(
            f"pass^k {entry['passk']} >= {route.min_passk} per {entry['run']} ({entry['date']})"
        ),
    )


def _identify(gw: Gateway, req: GenerateIn, presented_key: str | None) -> str | None:
    """A valid key authenticates the account; a contradicting caller
    field is a 400; an unknown key is a 401, never anonymous."""
    if presented_key is None:
        return req.caller
    account = resolve_account(gw.accounts, presented_key)
    if account is None:
        raise HTTPException(401, detail="unknown API key")
    if req.caller is not None and req.caller != account:
        raise HTTPException(
            400, detail=f"caller field {req.caller!r} contradicts the key's account {account!r}"
        )
    return account


def _check_balance(gw: Gateway, account: str | None) -> None:
    """The wallet's refusal: 402 `payment_required` — "you spent YOUR
    balance", a different sentence from budget_503's "you hit OUR
    cap". Only authenticated accounts with a grant are metered."""
    if account is None or account not in gw.accounts:
        return
    granted = gw.accounts[account]["granted_usd"]
    state = gw.wallet.state(account, granted)
    if state.remaining_usd <= 0:
        raise HTTPException(
            402,
            detail=f"payment_required: account {account!r} spent its balance "
            f"(${state.spent_usd:.4f} of ${granted:.4f} granted)",
        )


def _policy_filter(gw: Gateway, caller: str | None, eids: list[str]) -> list[str]:
    """The operator plane: a caller with a policy entry reaches only what
    it names; a caller without one is unrestricted. Policy binds pins too
    — operator authority outranks every caller word. NOTE: `caller` is
    self-declared attribution until the identity workstream binds it to a
    key; the mechanism is real, the authentication arrives with W4."""
    if caller is None or caller not in gw.policy:
        return eids
    allowed = gw.policy[caller]
    kept = [e for e in eids if policy_allows(allowed, e, gw.setups[e])]
    if not kept:
        raise HTTPException(
            403, detail=f"policy for caller {caller!r} allows none of these endpoints"
        )
    return kept


def _narrow(req: GenerateIn, eids: list[str]) -> list[str]:
    """Caller narrowing: `only`/`ignore` shrink the candidate set and can
    never grow it — an `only` naming something outside the routed
    candidates adds nothing (narrow-never-broaden, the suppressor shape)."""
    kept = list(eids)
    if req.only:
        sel = set(req.only)
        kept = [e for e in kept if sel & {e, e.split("@", 1)[0]}]
    if req.ignore:
        drop = set(req.ignore)
        kept = [e for e in kept if not (drop & {e, e.split("@", 1)[0]})]
    if not kept:
        raise HTTPException(400, detail="only/ignore narrowed the candidate set to nothing")
    return kept


def _price_ceiling(gw: Gateway, req: GenerateIn, eids: list[str]) -> list[str]:
    """`max_price` is HARD: if nothing fits the ceiling, refuse loudly
    with the cheapest available price named — never serve above a stated
    ceiling (their crispest design idea, kept verbatim)."""
    if req.max_price is None:
        return eids
    priced = {e: endpoint_price(gw.setups[e], PRICES_PER_MTOK) or 0.0 for e in eids}
    kept = [e for e in eids if priced[e] <= req.max_price]
    if not kept:
        cheapest = min(priced.values())
        raise HTTPException(
            400,
            detail=f"max_price {req.max_price}/mtok refused: cheapest "
            f"admitted endpoint costs {cheapest}/mtok",
        )
    return kept


def _misses_preference(gw: Gateway, req: GenerateIn, eid: str) -> bool:
    """An unmeasured endpoint cannot miss a preference; checked at p50."""
    b = gw.backend(eid)
    if req.preferred_max_latency is not None:
        p50 = b.ttft.percentile(50)
        if p50 is not None and p50 > req.preferred_max_latency:
            return True
    if req.preferred_min_throughput is not None:
        p50 = b.tps.percentile(50)
        if p50 is not None and p50 < req.preferred_min_throughput:
            return True
    return False


def _strict_sort(gw: Gateway, req: GenerateIn, eids: list[str]) -> list[str]:
    """A `sort` override is the either/or rule: the caller's list or the
    market lottery, never a blend — load balancing is disabled entirely
    (the documented behavior, adopted as-is)."""
    if req.sort == "price":
        return sorted(eids, key=lambda e: endpoint_price(gw.setups[e], PRICES_PER_MTOK) or 0.0)
    if req.sort == "latency":
        # unmeasured sorts last under a strict sort: the caller asked for
        # proven speed, and an empty window proves nothing
        return sorted(
            eids,
            key=lambda e: (
                gw.backend(e).ttft.percentile(50) is None,
                gw.backend(e).ttft.percentile(50) or 0.0,
            ),
        )
    return sorted(
        eids,
        key=lambda e: (
            gw.backend(e).tps.percentile(50) is None,
            -(gw.backend(e).tps.percentile(50) or 0.0),
        ),
    )


def _refuse_sunset(gw: Gateway, eid: str, today: str, *, pinned: bool) -> None:
    """410 Gone past sunset — never a silent remap: the name keeps its
    meaning and the refusal names the dates."""
    state = lifecycle_state(gw.setups[eid], today)
    if state == "sunset":
        lc = gw.setups[eid].get("lifecycle") or {}
        who = "pinned " if pinned else ""
        raise HTTPException(
            410,
            detail=f"{who}{eid!r} is past its sunset ({lc.get('sunset')}); "
            "nothing is substituted under a retired name",
        )
    if state == "deprecated":
        log.warning(
            "[gateway:lifecycle] %s is deprecated (sunset %s)",
            eid,
            (gw.setups[eid].get("lifecycle") or {}).get("sunset", "unscheduled"),
        )


def _endpoint_plan(gw: Gateway, decision: Any, req: GenerateIn) -> tuple[str, list[str]]:
    """Resolve the decision's target into (first endpoint, remaining
    within-alias candidates), applying the full constraint pipeline:
    policy (403) → narrowing (400) → capability (400) → price ceiling
    (400) → health tiers → strict sort or the price lottery with soft
    preferences deprioritizing.

    A pin composes with the hard constraints only; narrowing, sort, and
    soft preferences are no-ops on a set of one."""
    target = decision.model
    wants_tools = bool(req.tools)
    today = datetime.now(UTC).date().isoformat()
    if decision.source == "pin":
        if target in gw.setups:
            _policy_filter(gw, req.caller, [target])
            _refuse_sunset(gw, target, today, pinned=True)
            reason = capability_mismatch(
                gw.setups[target], wants_tools=wants_tools, max_tokens=req.max_tokens
            )
            if reason is not None:
                raise HTTPException(400, detail=f"pinned {target!r} cannot serve: {reason}")
            _price_ceiling(gw, req, [target])
            return target, []
        ids = gw.aliases.get(target)
        if ids and len(ids) > 1:
            raise HTTPException(
                400, detail=f"pin {target!r} is ambiguous across endpoints {ids}; pin one of them"
            )
        raise HTTPException(400, detail=f"unknown model alias {target!r}")
    if target in gw.setups and target not in gw.aliases:
        candidates = [target]  # an override may name an endpoint id directly
    else:
        candidates = gw.candidates(target)
        if not candidates:
            raise HTTPException(400, detail=f"unknown model alias {target!r}")
    candidates = _narrow(req, _policy_filter(gw, req.caller, candidates))
    admitted, reasons = [], []
    live = []
    for eid in candidates:
        state = lifecycle_state(gw.setups[eid], today)
        if state == "sunset":
            reasons.append(f"{eid}: sunset since {gw.setups[eid]['lifecycle']['sunset']}")
            continue
        if state == "deprecated":
            log.warning(
                "[gateway:lifecycle] serving deprecated %s (sunset %s)",
                eid,
                (gw.setups[eid].get("lifecycle") or {}).get("sunset", "unscheduled"),
            )
        live.append(eid)
    if not live:
        raise HTTPException(
            410, detail="every candidate endpoint is past sunset: " + "; ".join(reasons)
        )
    candidates = live
    for eid in candidates:
        reason = capability_mismatch(
            gw.setups[eid], wants_tools=wants_tools, max_tokens=req.max_tokens
        )
        if reason is None:
            admitted.append(eid)
        else:
            reasons.append(f"{eid}: {reason}")
    if not admitted:
        raise HTTPException(400, detail="no endpoint can serve this request: " + "; ".join(reasons))
    admitted = _price_ceiling(gw, req, admitted)
    up = [e for e in admitted if gw.backend(e).health.state != "down"] or admitted
    if req.sort is not None:
        ordered = _strict_sort(gw, req, up)
    else:
        ordered = _order_candidates(gw, up, misses=lambda e: _misses_preference(gw, req, e))
    return ordered[0], ordered[1:]


def _order_candidates(
    gw: Gateway, eids: list[str], misses: Callable[[str], bool] | None = None
) -> list[str]:
    """Free endpoints first, then priced grouped by (misses-preference,
    soft-deprioritized, health) and price-sampled within each group."""
    missed = misses or (lambda e: False)
    free = [e for e in eids if not endpoint_price(gw.setups[e], PRICES_PER_MTOK)]
    priced = [e for e in eids if e not in free]
    free.sort(key=lambda e: (missed(e), *gw.backend(e).sort_key()))
    groups: dict[tuple[bool, bool, bool], list[str]] = {}
    for e in priced:
        b = gw.backend(e)
        k = (missed(e), b.errors.soft_deprioritized(), b.health.state != "healthy")
        groups.setdefault(k, []).append(e)
    ordered = list(free)
    for k in sorted(groups):
        ordered.extend(_sample_by_inverse_square_price(gw, groups[k]))
    return ordered


def _sample_by_inverse_square_price(gw: Gateway, eids: list[str]) -> list[str]:
    """Weighted order without replacement, weight 1/price² — the
    documented market mechanism: at $1/$2/$3 the cheapest is 9× likelier
    than the priciest to go first, and the expensive one never starves.
    A lottery, not a sort: a hard sort starves the endpoints it ranks
    last, and a starved endpoint is one you learn nothing about."""
    pool = list(eids)
    out: list[str] = []
    while pool:
        weights = [1.0 / (endpoint_price(gw.setups[e], PRICES_PER_MTOK) or 1.0) ** 2 for e in pool]
        draw = gw.rng.random() * sum(weights)
        acc = 0.0
        for e, w in zip(pool, weights, strict=True):
            acc += w
            if draw <= acc:
                out.append(e)
                pool.remove(e)
                break
        else:
            out.append(pool.pop())
    return out


def _open_stream(
    gw: Gateway, alias: str, req: GenerateIn, factory: StreamFactory
) -> tuple[Backend, Iterator[StreamEvent]]:
    backend = gw.backend(alias)
    backend.admit()
    try:
        return backend, factory(alias, _request_kwargs(gw, alias, req))
    except Exception:
        backend.release()
        raise


def _default_stream_factory(gw: Gateway) -> StreamFactory:
    """Streams go through the seam's client — one payload builder for
    streamed and non-streamed calls, so a streamed request keeps every
    setup knob (extra_args above all, the constrained-decoding channel)."""

    def open_stream(alias: str, kwargs: dict[str, Any]) -> Iterator[StreamEvent]:
        if gw.setups[alias].get("provider") in UNSTREAMABLE_PROVIDERS:
            raise HTTPException(
                501, detail="anthropic streaming lands with its adapter; send stream=false"
            )
        return parse_chat_sse(gw.client(alias).messages.stream_lines(**kwargs))

    return open_stream


def probe(gw: Gateway, alias: str) -> ProbeResult:
    """A tiny real generation, bypassing the queue — a server can 200 its
    health endpoint while generation has collapsed."""
    setup = gw.setups[alias]
    started = time.monotonic()
    try:
        gw.client(alias).messages.create(
            model=setup["model"], max_tokens=5, messages=list(PROBE_PROMPT)
        )
    except Exception:
        return "fail"
    return "slow" if time.monotonic() - started > PROBE_SLOW_S else "ok"


def _probe_cadence(gw: Gateway, alias: str) -> float:
    return float(gw.setups[alias].get("probe_interval_s", DEFAULT_PROBE_INTERVAL_S))


def _probeable(gw: Gateway) -> dict[str, str]:
    """Routed providers whose setup declares `probe_interval_s` — probing
    is opt-in per setup. Undeclared means never probed: uptime spends
    nothing by default, and failures surface per-request through the
    walk; declaring a probe on a priced setup is a deliberate spend."""
    return {p: a for p, a in gw.routed().items() if "probe_interval_s" in gw.setups[a]}


def probe_tick(gw: Gateway) -> float | None:
    """The scheduler's wake granularity: the fastest configured cadence,
    None when nothing is probeable."""
    cadences = [_probe_cadence(gw, alias) for alias in _probeable(gw).values()]
    return min(cadences) if cadences else None


def run_probe_round(gw: Gateway, now: float | None = None) -> dict[str, ProbeResult]:
    """One pass over the probeable backends that are due, feeding the
    health machine. State transitions are logged — recovery is gradual by
    the health rules, so the readmission path is visible in the log, not
    inferred. The clock is injectable; per-provider due-ness lives here so
    the loop shell stays logic-free."""
    now = time.monotonic() if now is None else now
    results: dict[str, ProbeResult] = {}
    for provider, alias in sorted(_probeable(gw).items()):
        last = gw.last_probe.get(provider)
        if last is not None and now - last < _probe_cadence(gw, alias):
            continue
        gw.last_probe[provider] = now
        result = probe(gw, alias)
        backend = gw.backend(alias)
        was = backend.health.state
        state = backend.health.observe(result)
        results[provider] = result
        if state != was:
            log.warning(
                "[gateway:health] backend=%s state=%s was=%s probe=%s", provider, state, was, result
            )
    return results


def _probe_loop(gw: Gateway, tick_s: float, stop: threading.Event) -> None:
    while not stop.wait(tick_s):
        run_probe_round(gw)


def _stream_observer(gw: Gateway, source: str, task_label: str) -> Callable[[dict[str, Any]], None]:
    """The relay's terminal payloads mapped to metrics — a module function
    so every branch (end, stream_error, disconnect) tests directly; the
    endpoint's closure was unreachable by TestClient, which buffers
    streams to completion."""

    def observe_stream(payload: dict[str, Any]) -> None:
        if payload["type"] == "end":
            labels = {
                "backend": payload["backend"],
                "source": payload["source"],
                "task_class": task_label,
            }
            obs.GATEWAY_REQUESTS.add(1, {**labels, "outcome": "stream_ok"})
            obs.GATEWAY_FALLBACK_CHAIN.record(len(payload["fallback_chain"]))
            if payload["ttft_s"] is not None:
                obs.GATEWAY_TTFT.record(
                    payload["ttft_s"], {"backend": payload["backend"], "cached": "false"}
                )
            if payload["tokens_per_s"] is not None:
                obs.GATEWAY_TOKENS_PER_S.record(
                    payload["tokens_per_s"], {"backend": payload["backend"]}
                )
            u = payload["usage"]
            cost = estimate_cost(
                {
                    "input": u.get("input_tokens", 0),
                    "output": u.get("output_tokens", 0),
                    "cache_read": 0,
                    "cache_creation": 0,
                },
                gw.setups[payload["model"]]["model"],
            )
            obs.GATEWAY_COST.record(cost, labels)
            if (
                payload["fallback_chain"]
                and gw.fallback_budget is not None
                and _paid(gw, payload["model"])
            ):
                gw.fallback_budget.charge(cost)
                obs.GATEWAY_FALLBACK_SPEND.add(cost, {"backend": payload["backend"]})
        elif payload["type"] == "disconnect":
            obs.GATEWAY_REQUESTS.add(
                1,
                {
                    "backend": payload["backend"],
                    "outcome": "client_disconnected",
                    "source": source,
                    "task_class": task_label,
                },
            )
        else:
            obs.GATEWAY_STREAM_ERRORS.add(
                1,
                {"tokens_bucket": "zero" if payload["tokens_emitted"] == 0 else "nonzero"},
            )
            # without this the chain histogram counts only survivors
            obs.GATEWAY_FALLBACK_CHAIN.record(len(payload.get("fallback_chain", [])))
            obs.GATEWAY_REQUESTS.add(
                1,
                {
                    "backend": payload["backend"],
                    "outcome": "stream_error",
                    "source": source,
                    "task_class": task_label,
                },
            )

    return observe_stream


def _locally_served(setup: dict[str, Any]) -> bool:
    host = urlsplit(setup.get("base_url") or "").hostname
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")  # nosec B104 — match, not bind


def create_app(
    models_path: Path = MODELS_PATH,
    client_factory: Callable[[dict[str, Any]], Any] = build_client,
    registry: Mapping[TaskClass, ClassRoute] | None = None,
    stream_factory: StreamFactory | None = None,
    fallback_budget: FallForwardBudget | None = None,
    ignore_probes: bool = False,
    policy_path: Path | None = None,
    quality_path: Path | None = None,
    accounts_path: Path | None = None,
    wallet: Wallet | None = None,
    usage_ledger: UsageLedger | None = None,
) -> FastAPI:
    table, aliases = expand(yaml.safe_load(models_path.read_text()) or {})
    resolved_policy_path = policy_path or POLICY_PATH
    policy = load_policy(resolved_policy_path.read_text()) if resolved_policy_path.exists() else {}
    resolved_quality_path = quality_path or QUALITY_PATH
    quality = (
        load_quality(resolved_quality_path.read_text()) if resolved_quality_path.exists() else {}
    )
    today = datetime.now(UTC).date().isoformat()
    for task_class_name, arms in quality.items():
        aged = stale(arms, list(arms), today, QUALITY_MAX_AGE_DAYS)
        if aged:
            # a re-measurement signal, never a routing input: serving an
            # arm produces no pass^k, so a stale score cannot refresh itself
            log.warning(
                "[gateway:quality] %s: evidence older than %d days for %s; re-run the arms",
                task_class_name,
                QUALITY_MAX_AGE_DAYS,
                sorted(aged),
            )
    resolved_accounts_path = accounts_path or ACCOUNTS_PATH
    accounts = (
        load_accounts(resolved_accounts_path.read_text()) if resolved_accounts_path.exists() else {}
    )
    for name, setup in table.items():
        validate_lifecycle(name, setup)
        cadence = setup.get("probe_interval_s")
        if cadence is not None and float(cadence) <= 0:
            # wait(0) spins: a hot probe loop, and a spend bug on a priced setup
            raise SystemExit(f"setup {name!r}: probe_interval_s must be > 0, got {cadence}")
        if endpoint_price(setup, PRICES_PER_MTOK) is None and not _locally_served(setup):
            # "no price on file is unmetered" is a convention for local
            # weights; on a hosted endpoint it silently zero-bills paid calls
            log.warning("endpoint %s is hosted but has no price on file: it meters $0", name)
    gw = Gateway(
        setups=table,
        client_factory=client_factory,
        registry=DEFAULT_REGISTRY if registry is None else registry,
        aliases=aliases,
        fallback_budget=fallback_budget,
        policy=policy,
        quality=quality,
        accounts=accounts,
        wallet=wallet or Wallet(),
        usage_ledger=usage_ledger or UsageLedger(),
    )
    factory = _default_stream_factory(gw) if stream_factory is None else stream_factory
    # a custom factory declares no unstreamable providers: the eager
    # refusal must not inherit the default factory's anthropic limit
    unstreamable = UNSTREAMABLE_PROVIDERS if stream_factory is None else frozenset()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        worker: threading.Thread | None = None
        tick = probe_tick(gw)
        # ignore_probes suppresses, never enables: the catalog stays the
        # only authority for what is probed and how often
        if not ignore_probes and tick is not None:
            worker = threading.Thread(target=_probe_loop, args=(gw, tick, stop), daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=5.0)

    app = FastAPI(title="resgraph-gateway", version="0.1.0", lifespan=lifespan)
    app.state.gateway = gw
    obs.init_metrics()
    app.mount("/metrics", make_asgi_app())
    obs.register_gateway_depth_reader(
        lambda: [(name, b.in_flight) for name, b in gw.backends.items()]
    )

    @app.get("/v1/models")  # registration is the use
    def models() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """The routable catalog from the registry as its single source:
        aliases, their endpoints' serving facts, declared capabilities,
        prices where the pricing table knows the wire model. No mutable
        \"latest\" aliases, by decision — pins are the platform's point."""
        routed_aliases = {r.model for r in [*gw.registry.values(), GLOBAL_DEFAULT_MODEL]}
        stats: dict[str, Any] = {}
        for eid, setup in gw.setups.items():
            key = eid if "@" in eid else setup.get("provider", "default")
            b = gw.backends.get(key)  # only materialized state; never create
            if b is not None:
                stats[eid] = {
                    "ttft_last_5m": b.ttft.snapshot(),
                    "tps_last_5m": b.tps.snapshot(),
                    "soft_deprioritized": b.errors.soft_deprioritized(),
                }
        return {"data": catalog_rows(gw.setups, gw.aliases, routed_aliases, PRICES_PER_MTOK, stats)}

    @app.get("/v1/usage")  # registration is the use
    def usage(  # pyright: ignore[reportUnusedFunction]
        caller: str | None = None, x_mgmt_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """The usage surface, served from the meter's own records —
        per (day, account, endpoint): requests, tokens, cost. Gated by
        the management key when one is configured: the key that spends
        is not the key that administers."""
        if not mgmt_authorized(x_mgmt_key):
            raise HTTPException(403, detail="management key required for the usage surface")
        return {"data": gw.usage_ledger.aggregate(account=caller)}

    @app.post("/v1/generate", response_model=GenerateOut)  # registration is the use
    def generate(  # pyright: ignore[reportUnusedFunction]
        req: GenerateIn, x_api_key: str | None = Header(default=None)
    ) -> GenerateOut | StreamingResponse:
        account = _identify(gw, req, x_api_key)
        req = req.model_copy(update={"caller": account})
        _check_balance(gw, account)
        flagged = screen(req.messages, req.system)
        if flagged:
            # observe, never block: this platform's own traffic carries
            # adversarial text as data by design. The caller field is
            # request data: strip newlines so it cannot forge log lines
            safe_caller = (account or "anonymous").replace("\r", "").replace("\n", "")
            log.warning("[gateway:screen] request matches %s (caller=%s)", flagged, safe_caller)
            obs.GATEWAY_SCREEN_FLAGS.add(1, {"caller": account or "anonymous"})
        decision = None
        if req.pin is None and req.model is None:
            decision = _quality_route(gw, req.task_class)
        if decision is None:
            decision = resolve(
                pin=req.pin, model=req.model, task_class=req.task_class, registry=gw.registry
            )
        first, within = _endpoint_plan(gw, decision, req)

        if req.stream:
            routed_backend = gw.backend(first)
            if routed_backend.health.state == "down":
                labels = {
                    "backend": routed_backend.name,
                    "source": decision.source,
                    "task_class": req.task_class or "none",
                }
                obs.GATEWAY_FALLBACK_CHAIN.record(0)
                if not decision.fallback_allowed:
                    obs.GATEWAY_REQUESTS.add(1, {**labels, "outcome": "pin_failed_502"})
                    raise HTTPException(
                        502,
                        detail=f"pinned {decision.model!r} backend {routed_backend.name!r} is down",
                    )
                if not within and _no_streamable_fallback(gw, first, unstreamable):
                    obs.GATEWAY_REQUESTS.add(1, {**labels, "outcome": "refused_503"})
                    raise HTTPException(
                        503,
                        detail=f"backend {routed_backend.name!r} is down and no streamable "
                        "fallback is up; retry after the next probe round",
                        headers={"Retry-After": str(max(1, int(_probe_cadence(gw, first))))},
                    )
            (backend, events), alias, chain = _serve_with_walk(
                gw,
                decision,
                lambda a: _open_stream(gw, a, req, factory),
                req.task_class or "none",
                first=first,
                more=within,
            )

            def reopen(chain2: list[str]) -> tuple[str, Backend, Iterator[StreamEvent]] | None:
                if not decision.fallback_allowed:
                    return None
                nxt = _next_alias(gw, chain2)
                while nxt is not None:
                    try:
                        b, ev = _open_stream(gw, nxt, req, factory)
                        return nxt, b, ev
                    except Exception as exc:
                        _record_hop(gw, nxt, exc, chain2)
                        nxt = _next_alias(gw, chain2)
                return None

            return StreamingResponse(
                relay(
                    alias=alias,
                    backend=backend,
                    events=events,
                    source=decision.source,
                    fallback_chain=chain,
                    reopen=reopen,
                    observe=_stream_observer(gw, decision.source, req.task_class or "none"),
                ),
                media_type="text/event-stream",
            )

        # The response cache answers only byte-identical repeats of
        # deterministic requests: a temperature-0 setup, non-streamed. A
        # sampled response replayed as the answer would be a quiet lie, so
        # anything else is a pass-through — and a hit says cached=true.
        key = None
        if req.cache_responses and gw.setups[first].get("temperature") == 0:
            key = cache_key(first, _request_kwargs(gw, first, req))
            hit = gw.cache.get(key)
            if hit is None:
                obs.GATEWAY_CACHE_MISSES.add(1, {"layer": "gateway"})
            if hit is not None:
                obs.GATEWAY_CACHE_HITS.add(1, {"layer": "gateway"})
                obs.GATEWAY_CACHE_TOKENS_SAVED.add(hit.usage.input_tokens + hit.usage.output_tokens)
                obs.GATEWAY_REQUESTS.add(
                    1,
                    {
                        "backend": hit.backend,
                        "outcome": "cached",
                        "source": decision.source,
                        "task_class": req.task_class or "none",
                    },
                )
                gw.usage_ledger.record(
                    account=account,
                    endpoint=hit.model,
                    model=gw.setups[hit.model].get("model"),
                    input_tokens=hit.usage.input_tokens,
                    output_tokens=hit.usage.output_tokens,
                    cost_usd=0.0,  # a cache hit spends no backend tokens
                    cached=True,
                )
                return hit.model_copy(update={"cached": True, "cost_usd": 0.0})
        (content, usage, latency), alias, chain = _serve_with_walk(
            gw,
            decision,
            lambda a: _call(gw, a, req),
            req.task_class or "none",
            first=first,
            more=within,
        )
        served_cost = _cost_of(gw, alias, usage)
        out = GenerateOut(
            content=content,
            model=alias,
            source=decision.source,
            backend=gw.backend(alias).name,
            fallback_chain=chain,
            latency_s=latency,
            usage=usage,
            cost_usd=served_cost,
        )
        if account is not None and account in gw.accounts:
            gw.wallet.charge(account, served_cost)
        gw.usage_ledger.record(
            account=account,
            endpoint=alias,
            model=gw.setups[alias].get("model"),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=served_cost,
        )
        if chain and gw.fallback_budget is not None and _paid(gw, alias):
            fallforward_cost = _cost_of(gw, alias, usage)
            gw.fallback_budget.charge(fallforward_cost)
            obs.GATEWAY_FALLBACK_SPEND.add(fallforward_cost, {"backend": out.backend})
        if req.cache_responses and gw.setups[alias].get("temperature") == 0:
            served_key = (
                key
                if alias == first and key is not None
                else cache_key(alias, _request_kwargs(gw, alias, req))
            )
            gw.cache.put(served_key, out, usage.input_tokens + usage.output_tokens)
        _observe_served(gw, alias, out, req.task_class or "none", "ok")
        return out

    return app
