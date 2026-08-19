"""Precedence router: which model serves a request, and why.

Resolution is pure and total over the request shape — pin → override →
task-class default → global default — and the winning ``source`` rides the
decision so cost questions are answered by a field, not a hunt.

``model`` values are served-model ALIASES — the setup names in
models.yaml — never raw provider ids: where an alias runs is its setup's
provider and base_url, so local vs remote stays transparent here. Backend
concerns — which queue, which adapter, health/EWMA tie-breaking — are
resolved from the setup at dispatch, never inferred from the name.

Decisions: D30 (SPEC.md).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Source = Literal["pin", "override", "task_class_default", "global_default"]
TaskClass = Literal["judgment", "workhorse", "classification"]


@dataclass(frozen=True)
class ClassRoute:
    """A task-class default: registry data with its rationale, not code."""

    model: str
    rationale: str


DEFAULT_REGISTRY: Mapping[TaskClass, ClassRoute] = {
    "judgment": ClassRoute(
        "haiku",
        "triage reasoning; the daily-driver setup chosen by the model arms",
    ),
    "workhorse": ClassRoute(
        "qwen-local-1.5b",
        "bulk/replay serving-shape traffic; the setup that fits this host",
    ),
    "classification": ClassRoute(
        "qwen-local-1.5b",
        "light classification calls; deterministic graders dominate, replay fills this class",
    ),
}

GLOBAL_DEFAULT_MODEL = ClassRoute("qwen-local-1.5b", "no signal from the request: fail cheap")


@dataclass(frozen=True)
class RouteDecision:
    model: str
    source: Source
    fallback_allowed: bool
    rationale: str


def resolve(
    *,
    pin: str | None = None,
    model: str | None = None,
    task_class: TaskClass | None = None,
    registry: Mapping[TaskClass, ClassRoute] | None = None,
) -> RouteDecision:
    """Resolve by precedence. A pin never falls back and never substitutes
    (a silently-rerouted judge is a corrupted baseline); the task-class
    vocabulary is closed, so an unknown class is a caller bug and raises
    rather than routing somewhere plausible — the type says so statically,
    the ValueError repeats it for wire data the types cannot vouch for."""
    table = DEFAULT_REGISTRY if registry is None else registry
    if pin:
        return RouteDecision(
            model=pin,
            source="pin",
            fallback_allowed=False,
            rationale="pinned: exact model, no fallback, no substitution",
        )
    if model:
        return RouteDecision(
            model=model,
            source="override",
            fallback_allowed=True,
            rationale="explicit model override",
        )
    if task_class is not None:
        route = table.get(task_class)
        if route is None:
            raise ValueError(f"unknown task_class {task_class!r}; have: {sorted(table)}")
        return RouteDecision(
            model=route.model,
            source="task_class_default",
            fallback_allowed=True,
            rationale=route.rationale,
        )
    g = GLOBAL_DEFAULT_MODEL
    return RouteDecision(
        model=g.model,
        source="global_default",
        fallback_allowed=True,
        rationale=g.rationale,
    )
