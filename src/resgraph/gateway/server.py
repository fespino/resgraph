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
anthropic-setup stream answers 501 until its adapter lands."""

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from resgraph.evals.providers import build_client
from resgraph.gateway.accounting import StreamAccount
from resgraph.gateway.cache import ResponseCache, cache_key
from resgraph.gateway.dispatch import Backend, ProbeResult, QueueFull, choose
from resgraph.gateway.relay import StreamEvent, StreamFactory, parse_chat_sse, relay
from resgraph.gateway.router import (
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT_MODEL,
    ClassRoute,
    Source,
    TaskClass,
    resolve,
)

log = logging.getLogger("resgraph.gateway")

MODELS_PATH = Path("evals/models.yaml")
PROVIDER_LIMITS: Mapping[str, tuple[int, int]] = {"anthropic": (8, 16)}
DEFAULT_LIMITS = (1, 4)
PROBE_SLOW_S = 5.0
PROBE_PROMPT = [{"role": "user", "content": "Reply with the single word: pong"}]


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
    cached: bool = False


@dataclass
class Gateway:
    """Setups, their clients, and one dispatch state per provider."""

    setups: dict[str, dict[str, Any]]
    client_factory: Callable[[dict[str, Any]], Any]
    registry: Mapping[TaskClass, ClassRoute] = field(default_factory=lambda: DEFAULT_REGISTRY)
    clients: dict[str, Any] = field(default_factory=dict)
    backends: dict[str, Backend] = field(default_factory=dict)
    cache: ResponseCache = field(default_factory=ResponseCache)

    def client(self, alias: str) -> Any:
        if alias not in self.clients:
            self.clients[alias] = self.client_factory({"name": alias, **self.setups[alias]})
        return self.clients[alias]

    def backend(self, alias: str) -> Backend:
        provider = self.setups[alias].get("provider", "default")
        if provider not in self.backends:
            concurrency, queue_max = PROVIDER_LIMITS.get(provider, DEFAULT_LIMITS)
            self.backends[provider] = Backend(provider, concurrency, queue_max)
        return self.backends[provider]

    def routed(self) -> dict[str, str]:
        """provider → serving alias, from the registry plus the global
        default. The registry is the gateway's serving authority; a setup it
        does not route is a catalog entry (an eval arm), never a backend to
        serve, walk to, or probe — an unrouted provider must not be spent on."""
        out: dict[str, str] = {}
        for route in [*self.registry.values(), GLOBAL_DEFAULT_MODEL]:
            setup = self.setups.get(route.model)
            if setup is not None:
                out.setdefault(setup.get("provider", "default"), route.model)
        return out

    def alias_for_backend(self, provider: str) -> str | None:
        return self.routed().get(provider)


def _wire(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    kind = getattr(block, "type", "text")
    if kind == "tool_use":
        return {"type": kind, "id": block.id, "name": block.name, "input": block.input}
    return {"type": kind, "text": getattr(block, "text", "")}


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
        if account.ttft is not None:
            backend.ttft_ewma.update(account.ttft)
        usage = UsageOut(
            input_tokens=int(getattr(resp.usage, "input_tokens", 0) or 0),
            output_tokens=out,
            cache_read_tokens=int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(resp.usage, "cache_creation_input_tokens", 0) or 0),
        )
        return [_wire(b) for b in resp.content], usage, account.ttft or 0.0
    finally:
        backend.release()


def _next_alias(gw: Gateway, chain: list[str]) -> str | None:
    """The next hop of the walk: an untried, not-down backend's serving
    alias, chosen by health then latency; None when the walk is exhausted.
    Candidates come from the routed providers only — the registry is the
    serving authority, and an unrouted catalog setup is never a hop."""
    tried = {c.split(":", 1)[0] for c in chain}
    candidates = []
    for provider, fallback in gw.routed().items():
        if provider in tried:
            continue
        backend = gw.backend(fallback)
        if backend.health.state != "down":
            candidates.append(backend)
    nxt = choose(candidates)
    return gw.alias_for_backend(nxt.name) if nxt else None


def _record_hop(gw: Gateway, alias: str, exc: Exception, chain: list[str]) -> None:
    failed_backend = gw.backend(alias).name
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
    gw: Gateway, decision: Any, attempt: Callable[[str], T]
) -> tuple[T, str, list[str]]:
    """Drive the init-failure walk for any attempt shape (a full call or a
    stream open): pins fail loudly, other traffic hops until served or
    exhausted."""
    chain: list[str] = []
    alias: str | None = decision.model
    while alias is not None:
        try:
            return attempt(alias), alias, chain
        except QueueFull as exc:
            raise HTTPException(
                429, detail=str(exc), headers={"Retry-After": str(exc.retry_after_s)}
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            if not decision.fallback_allowed:
                raise HTTPException(
                    502, detail=f"pinned {alias!r} failed on {gw.backend(alias).name}: {exc}"
                ) from exc
            _record_hop(gw, alias, exc, chain)
            alias = _next_alias(gw, chain)
    log.error("[gateway:exhausted] chain=%s", chain)
    raise HTTPException(503, detail=f"no backend could serve; chain={chain}")


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
        if gw.setups[alias].get("provider") == "anthropic":
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


def run_probe_round(gw: Gateway) -> dict[str, ProbeResult]:
    """One probe pass over every backend, feeding the health machine.
    State transitions are logged — recovery is gradual by the health rules,
    so the readmission path is visible in the log, not inferred."""
    results: dict[str, ProbeResult] = {}
    for provider, alias in sorted(gw.routed().items()):
        result = probe(gw, alias)
        backend = gw.backend(alias)
        was = backend.health.state
        now = backend.health.observe(result)
        results[provider] = result
        if now != was:
            log.warning(
                "[gateway:health] backend=%s state=%s was=%s probe=%s", provider, now, was, result
            )
    return results


def _probe_loop(gw: Gateway, interval_s: float, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        run_probe_round(gw)


def create_app(
    models_path: Path = MODELS_PATH,
    client_factory: Callable[[dict[str, Any]], Any] = build_client,
    registry: Mapping[TaskClass, ClassRoute] | None = None,
    stream_factory: StreamFactory | None = None,
    probe_interval_s: float | None = None,
) -> FastAPI:
    setups = yaml.safe_load(models_path.read_text()) or {}
    gw = Gateway(
        setups=setups,
        client_factory=client_factory,
        registry=DEFAULT_REGISTRY if registry is None else registry,
    )
    factory = _default_stream_factory(gw) if stream_factory is None else stream_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        worker: threading.Thread | None = None
        if probe_interval_s is not None:
            worker = threading.Thread(
                target=_probe_loop, args=(gw, probe_interval_s, stop), daemon=True
            )
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=5.0)

    app = FastAPI(title="resgraph-gateway", version="0.1.0", lifespan=lifespan)
    app.state.gateway = gw

    @app.post("/v1/generate", response_model=GenerateOut)  # registration is the use
    def generate(req: GenerateIn) -> GenerateOut | StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        decision = resolve(pin=req.pin, model=req.model, task_class=req.task_class)
        if decision.model not in gw.setups:
            raise HTTPException(400, detail=f"unknown model alias {decision.model!r}")

        if req.stream:
            (backend, events), alias, chain = _serve_with_walk(
                gw, decision, lambda a: _open_stream(gw, a, req, factory)
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
                ),
                media_type="text/event-stream",
            )

        # The response cache answers only byte-identical repeats of
        # deterministic requests: a temperature-0 setup, non-streamed. A
        # sampled response replayed as the answer would be a quiet lie, so
        # anything else is a pass-through — and a hit says cached=true.
        key = None
        if req.cache_responses and gw.setups[decision.model].get("temperature") == 0:
            key = cache_key(decision.model, _request_kwargs(gw, decision.model, req))
            hit = gw.cache.get(key)
            if hit is not None:
                return hit.model_copy(update={"cached": True})
        (content, usage, latency), alias, chain = _serve_with_walk(
            gw, decision, lambda a: _call(gw, a, req)
        )
        out = GenerateOut(
            content=content,
            model=alias,
            source=decision.source,
            backend=gw.backend(alias).name,
            fallback_chain=chain,
            latency_s=latency,
            usage=usage,
        )
        if req.cache_responses and gw.setups[alias].get("temperature") == 0:
            served_key = (
                key
                if alias == decision.model and key is not None
                else cache_key(alias, _request_kwargs(gw, alias, req))
            )
            gw.cache.put(served_key, out, usage.input_tokens + usage.output_tokens)
        return out

    return app
