"""Precedence router: which backend+model serves a request, and why.

Resolution is pure and total over the request shape — pin → override →
task-class default → global default — and the winning ``source`` rides the
decision so cost questions are answered by a field, not a hunt. Health- and
EWMA-latency tie-breaking among eligible backends happens at dispatch, not
here, so the whole table is exercised offline."""

from dataclasses import dataclass

PIN = "pin"
OVERRIDE = "override"
TASK_CLASS_DEFAULT = "task_class_default"
GLOBAL_DEFAULT = "global_default"

LOCAL = "local"
ANTHROPIC = "anthropic"

JUDGMENT = "judgment"
WORKHORSE = "workhorse"
CLASSIFICATION = "classification"


@dataclass(frozen=True)
class ClassRoute:
    """A task-class default: registry data with its rationale, not code."""

    backend: str
    model: str
    rationale: str


DEFAULT_REGISTRY: dict[str, ClassRoute] = {
    JUDGMENT: ClassRoute(
        ANTHROPIC,
        "claude-haiku-4-5",
        "triage reasoning; the daily-driver model chosen by the model arms",
    ),
    WORKHORSE: ClassRoute(
        LOCAL,
        "qwen2.5:1.5b",
        "bulk/replay serving-shape traffic; the model that fits this host",
    ),
    CLASSIFICATION: ClassRoute(
        LOCAL,
        "qwen2.5:1.5b",
        "light classification calls; deterministic graders dominate, replay fills this class",
    ),
}

GLOBAL_DEFAULT_MODEL = ClassRoute(LOCAL, "qwen2.5:1.5b", "no signal from the request: fail cheap")


@dataclass(frozen=True)
class RouteDecision:
    backend: str
    model: str
    source: str
    fallback_allowed: bool
    rationale: str


def backend_of(model: str) -> str:
    """A model id names its backend: ``claude-*`` is Anthropic, anything else local."""
    return ANTHROPIC if model.startswith("claude-") else LOCAL


def resolve(
    *,
    pin: str | None = None,
    model: str | None = None,
    task_class: str | None = None,
    registry: dict[str, ClassRoute] | None = None,
) -> RouteDecision:
    """Resolve by precedence. A pin never falls back and never substitutes
    (a silently-rerouted judge is a corrupted baseline); an unknown task
    class is a caller bug and raises rather than routing somewhere
    plausible."""
    table = DEFAULT_REGISTRY if registry is None else registry
    if pin:
        return RouteDecision(
            backend=backend_of(pin),
            model=pin,
            source=PIN,
            fallback_allowed=False,
            rationale="pinned: exact model, no fallback, no substitution",
        )
    if model:
        return RouteDecision(
            backend=backend_of(model),
            model=model,
            source=OVERRIDE,
            fallback_allowed=True,
            rationale="explicit model override",
        )
    if task_class is not None:
        route = table.get(task_class)
        if route is None:
            raise ValueError(f"unknown task_class {task_class!r}; have: {sorted(table)}")
        return RouteDecision(
            backend=route.backend,
            model=route.model,
            source=TASK_CLASS_DEFAULT,
            fallback_allowed=True,
            rationale=route.rationale,
        )
    g = GLOBAL_DEFAULT_MODEL
    return RouteDecision(
        backend=g.backend,
        model=g.model,
        source=GLOBAL_DEFAULT,
        fallback_allowed=True,
        rationale=g.rationale,
    )
