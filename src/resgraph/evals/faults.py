"""Induced faults for the degraded items (#152).

The drill kills the hot store mid-run. Rather than listing which tools
read it — a list that rots the moment a tool is added — the fault is
injected at the store handle: after N tool calls the hot session
factory raises, so every hot-backed tool fails through its own error
path and the cold-backed ones keep answering.

That division is the thing under test. `resource_history` and
`world_diff` read the cold store, so a well-harnessed agent is not
blind when the graph dies: it finishes with history-only triage and
says so. An agent that instead keeps asserting live topology is
fabricating, and the evidence dimension catches it.
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
    """A QueryContext factory that stops serving hot sessions after
    `calls` tool calls. The toolset builds one context per tool call,
    so counting contexts counts calls."""
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
