"""Transport-injected caller identity.

The LLM never sees this: it is keyword-only on every canonical body and
absent from the LLM-facing schema, so a caller cannot supply its own
authority.
"""

from dataclasses import dataclass
from typing import Literal

from resgraph.query.executor import QueryContext

Caller = Literal["mcp", "http", "analyst"]


@dataclass(frozen=True)
class CallerContext:
    caller: Caller
    scopes: frozenset[str]
    query: QueryContext
