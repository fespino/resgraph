"""Induced faults for the degraded items (#152).

The fault goes at the store handle rather than per tool: after N tool
calls the hot session factory raises, so hot-backed tools fail through
their own error paths and cold-backed ones keep answering. Rationale
in SPEC.md (D29a addendum).
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
    """The toolset builds one context per tool call, so counting
    contexts counts calls."""
    seen = {"n": 0}

    def dead() -> Any:
        raise RuntimeError(HOT_STORE_DEAD)

    def factory() -> QueryContext:
        seen["n"] += 1
        alive = seen["n"] <= calls
        return QueryContext(
            session_factory=session_factory if alive else dead,
            catalog_factory=catalog_factory,
        )

    return factory
