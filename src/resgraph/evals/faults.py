"""Induced faults for the degraded items (#152, #158).

The fault goes at the store handle rather than per tool: after N
acquisitions of the targeted store the factory raises, so tools backed
by it fail through their own error paths and tools backed by the other
store keep answering. Counting acquisitions of the targeted store
rather than tool calls is what makes the fault observable — INC-002
measured nothing for want of that distinction. Rationale in SPEC.md
(D29a addendum).
"""

from collections.abc import Callable
from typing import Any

from resgraph.query.executor import QueryContext

HOT_STORE_DEAD = "hot store unavailable (induced: analyst degraded drill)"
COLD_STORE_DEAD = "cold store unavailable (induced: analyst degraded drill)"


def _dies_after(factory: Callable[[], Any], calls: int, dead: str) -> Callable[[], Any]:
    seen = {"n": 0}

    def acquire() -> Any:
        seen["n"] += 1
        if seen["n"] > calls:
            raise RuntimeError(dead)
        return factory()

    return acquire


def hot_store_dies_after(
    calls: int,
    *,
    session_factory: Callable[[], Any],
    catalog_factory: Callable[[], Any],
) -> Callable[[], QueryContext]:
    """The kill lands on the next call that actually reaches for the
    graph; a cold-only call after it is unaffected, by design."""
    hot = _dies_after(session_factory, calls, HOT_STORE_DEAD)

    def factory() -> QueryContext:
        return QueryContext(session_factory=hot, catalog_factory=catalog_factory)

    return factory


def cold_store_dies_after(
    calls: int,
    *,
    session_factory: Callable[[], Any],
    catalog_factory: Callable[[], Any],
) -> Callable[[], QueryContext]:
    """The inverse kill (#158): history dies, the live graph keeps
    answering. Time-travel triage reads cold by construction, so this
    is the fault aimed at what the workload actually depends on."""
    cold = _dies_after(catalog_factory, calls, COLD_STORE_DEAD)

    def factory() -> QueryContext:
        return QueryContext(session_factory=session_factory, catalog_factory=cold)

    return factory
