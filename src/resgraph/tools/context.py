"""Transport-injected caller identity.

The LLM never sees this: it is keyword-only on every canonical body and
absent from the LLM-facing schema, so a caller cannot supply its own
authority.

`emit` is the write channel to the ingest stream. Read tools never look
at it and no read transport supplies one, so the privileged capability
is gated by what its caller was handed rather than by a flag it could
be talked into setting (D26).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from resgraph.query.executor import QueryContext
from resgraph.schema import UpdateMessage

Caller = Literal["mcp", "http", "analyst", "operator"]


@dataclass(frozen=True)
class CallerContext:
    caller: Caller
    scopes: frozenset[str]
    query: QueryContext
    emit: Callable[[list[UpdateMessage]], None] | None = None
