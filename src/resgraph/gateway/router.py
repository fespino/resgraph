"""Precedence router: which worker serves a request, and why.

Resolution is pure and total over the request shape — pin → override →
task-class default → global default — and the winning ``source`` rides the
decision so cost questions are answered by a field, not a hunt.

The router speaks worker NAMES (the setups in workers.yaml), the same
vocabulary as ``--worker``: where a worker runs is its setup's provider and
base_url, so local vs remote stays transparent here. Backend concerns —
which queue, which adapter, health/EWMA tie-breaking — are resolved from
the setup at dispatch, never inferred from the name."""

from dataclasses import dataclass

PIN = "pin"
OVERRIDE = "override"
TASK_CLASS_DEFAULT = "task_class_default"
GLOBAL_DEFAULT = "global_default"

JUDGMENT = "judgment"
WORKHORSE = "workhorse"
CLASSIFICATION = "classification"


@dataclass(frozen=True)
class ClassRoute:
    """A task-class default: registry data with its rationale, not code."""

    worker: str
    rationale: str


DEFAULT_REGISTRY: dict[str, ClassRoute] = {
    JUDGMENT: ClassRoute(
        "haiku",
        "triage reasoning; the daily-driver worker chosen by the model arms",
    ),
    WORKHORSE: ClassRoute(
        "qwen-local-1.5b",
        "bulk/replay serving-shape traffic; the worker that fits this host",
    ),
    CLASSIFICATION: ClassRoute(
        "qwen-local-1.5b",
        "light classification calls; deterministic graders dominate, replay fills this class",
    ),
}

GLOBAL_DEFAULT_WORKER = ClassRoute("qwen-local-1.5b", "no signal from the request: fail cheap")


@dataclass(frozen=True)
class RouteDecision:
    worker: str
    source: str
    fallback_allowed: bool
    rationale: str


def resolve(
    *,
    pin: str | None = None,
    worker: str | None = None,
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
            worker=pin,
            source=PIN,
            fallback_allowed=False,
            rationale="pinned: exact worker, no fallback, no substitution",
        )
    if worker:
        return RouteDecision(
            worker=worker,
            source=OVERRIDE,
            fallback_allowed=True,
            rationale="explicit worker override",
        )
    if task_class is not None:
        route = table.get(task_class)
        if route is None:
            raise ValueError(f"unknown task_class {task_class!r}; have: {sorted(table)}")
        return RouteDecision(
            worker=route.worker,
            source=TASK_CLASS_DEFAULT,
            fallback_allowed=True,
            rationale=route.rationale,
        )
    g = GLOBAL_DEFAULT_WORKER
    return RouteDecision(
        worker=g.worker,
        source=GLOBAL_DEFAULT,
        fallback_allowed=True,
        rationale=g.rationale,
    )
