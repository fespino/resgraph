"""Induced faults for the degraded items (#152).

The fault goes at the store handle rather than per tool: after N hot
sessions the factory raises, so hot-backed tools fail through their own
error paths and cold-backed ones keep answering. Counting HOT
acquisitions rather than tool calls is what makes the fault observable
— INC-002 measured nothing for want of that distinction. Rationale in
SPEC.md (D29a addendum).
"""

from collections.abc import Callable
from typing import Any

from resgraph.query.executor import QueryContext

HOT_STORE_DEAD = "hot store unavailable (induced: analyst degraded drill)"


def hot_store_dies_after(
    calls: int,
    *,
    session_factory: Callable[[], Any],
    catalog_factory: Callable[[], Any],
) -> Callable[[], QueryContext]:
    """The kill lands on the next call that actually reaches for the
    graph; a cold-only call after it is unaffected, by design."""
    seen = {"n": 0}

    def hot() -> Any:
        seen["n"] += 1
        if seen["n"] > calls:
            raise RuntimeError(HOT_STORE_DEAD)
        return session_factory()

    def factory() -> QueryContext:
        return QueryContext(session_factory=hot, catalog_factory=catalog_factory)

    return factory
