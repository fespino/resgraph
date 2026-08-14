"""The gateway process: one `/v1/generate` fronting the named setups.

Requests resolve through the precedence router, admit through the bounded
per-backend queues, and are served by the same clients the eval seam builds —
the setup's provider decides the backend, never the alias's spelling. The
winning source, the serving backend, and any fallback chain ride every
response.

Failure semantics (non-streamed, so every failure is before the first
token): a pin fails loudly and never substitutes; other traffic walks to
the next eligible backend, each hop logged with a `[gateway:fallback]` tag
and the walk alerted once the chain exceeds one hop; an exhausted walk is a
clean 503 tagged `[gateway:exhausted]`. Streaming returns 501 until the
relay lands."""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from resgraph.evals.providers import build_client
from resgraph.gateway.accounting import StreamAccount
from resgraph.gateway.dispatch import Backend, ProbeResult, QueueFull, choose
from resgraph.gateway.router import DEFAULT_REGISTRY, ClassRoute, TaskClass, resolve

log = logging.getLogger("resgraph.gateway")

MODELS_PATH = Path("evals/models.yaml")
PROVIDER_LIMITS: Mapping[str, tuple[int, int]] = {"anthropic": (8, 16)}
DEFAULT_LIMITS = (1, 4)
PROBE_SLOW_S = 5.0
PROBE_PROMPT = [{"role": "user", "content": "Reply with the single word: pong"}]


class GenerateIn(BaseModel):
    messages: list[dict[str, Any]]
    system: str | None = None
    tools: list[dict[str, Any]] | None = None
    max_tokens: int = 1024
    task_class: TaskClass | None = None
    model: str | None = None
    pin: str | None = None
    stream: bool = False


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int


class GenerateOut(BaseModel):
    content: list[dict[str, Any]]
    model: str
    source: str
    backend: str
    fallback_chain: list[str]
    latency_s: float
    usage: UsageOut


@dataclass
class Gateway:
    """Setups, their clients, and one dispatch state per provider."""

    setups: dict[str, dict[str, Any]]
    client_factory: Callable[[dict[str, Any]], Any]
    registry: Mapping[TaskClass, ClassRoute] = field(default_factory=lambda: DEFAULT_REGISTRY)
    clients: dict[str, Any] = field(default_factory=dict)
    backends: dict[str, Backend] = field(default_factory=dict)

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

    def alias_for_backend(self, provider: str) -> str | None:
        """The setup a fallback hop serves on a given backend — the first
        registry route living there, else any setup that does."""
        for route in self.registry.values():
            if self.setups.get(route.model, {}).get("provider", "default") == provider:
                return route.model
        for alias, setup in self.setups.items():
            if setup.get("provider", "default") == provider:
                return alias
        return None


def _wire(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    kind = getattr(block, "type", "text")
    if kind == "tool_use":
        return {"type": kind, "id": block.id, "name": block.name, "input": block.input}
    return {"type": kind, "text": getattr(block, "text", "")}


def _call(gw: Gateway, alias: str, req: GenerateIn) -> tuple[list[dict[str, Any]], UsageOut, float]:
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
            input_tokens=int(getattr(resp.usage, "input_tokens", 0) or 0), output_tokens=out
        )
        return [_wire(b) for b in resp.content], usage, account.ttft or 0.0
    finally:
        backend.release()


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


def create_app(
    models_path: Path = MODELS_PATH,
    client_factory: Callable[[dict[str, Any]], Any] = build_client,
    registry: Mapping[TaskClass, ClassRoute] | None = None,
) -> FastAPI:
    setups = yaml.safe_load(models_path.read_text()) or {}
    gw = Gateway(
        setups=setups,
        client_factory=client_factory,
        registry=DEFAULT_REGISTRY if registry is None else registry,
    )
    app = FastAPI(title="resgraph-gateway", version="0.1.0")
    app.state.gateway = gw

    @app.post("/v1/generate", response_model=GenerateOut)  # registration is the use
    def generate(req: GenerateIn) -> GenerateOut:  # pyright: ignore[reportUnusedFunction]
        if req.stream:
            raise HTTPException(501, detail="streaming lands with the relay; send stream=false")
        decision = resolve(pin=req.pin, model=req.model, task_class=req.task_class)
        if decision.model not in gw.setups:
            raise HTTPException(400, detail=f"unknown model alias {decision.model!r}")

        chain: list[str] = []
        alias: str | None = decision.model
        while alias is not None:
            try:
                content, usage, latency = _call(gw, alias, req)
            except QueueFull as exc:
                raise HTTPException(
                    429,
                    detail=str(exc),
                    headers={"Retry-After": str(exc.retry_after_s)},
                ) from exc
            except Exception as exc:
                failed_backend = gw.backend(alias).name
                if not decision.fallback_allowed:
                    raise HTTPException(
                        502, detail=f"pinned {alias!r} failed on {failed_backend}: {exc}"
                    ) from exc
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
                tried = {c.split(":", 1)[0] for c in chain}
                candidates = []
                for provider in {s.get("provider", "default") for s in gw.setups.values()}:
                    fallback = gw.alias_for_backend(provider)
                    if provider in tried or fallback is None:
                        continue
                    backend = gw.backend(fallback)
                    if backend.health.state != "down":
                        candidates.append(backend)
                nxt = choose(candidates)
                alias = gw.alias_for_backend(nxt.name) if nxt else None
                continue
            return GenerateOut(
                content=content,
                model=alias,
                source=decision.source,
                backend=gw.backend(alias).name,
                fallback_chain=chain,
                latency_s=latency,
                usage=usage,
            )
        log.error("[gateway:exhausted] chain=%s", chain)
        raise HTTPException(503, detail=f"no backend could serve; chain={chain}")

    return app
