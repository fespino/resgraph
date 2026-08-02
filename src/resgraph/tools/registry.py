"""TOOL_REGISTRY — the single grep-able source of truth for the tool
surface. Both transports derive from it; the drift guard asserts
nothing exists outside it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from resgraph.tools.canonical.entity import FetchResourceIn, FetchResourceOut, fetch_resource
from resgraph.tools.canonical.history import (
    ResourceHistoryIn,
    ResourceHistoryOut,
    WorldDiffIn,
    WorldDiffOut,
    resource_history,
    world_diff,
)
from resgraph.tools.canonical.traversal import (
    BlastRadiusIn,
    BlastRadiusOut,
    DependencyPathIn,
    DependencyPathOut,
    blast_radius,
    dependency_path,
)

Surface = Literal["mcp", "http"]
ErrorClass = Literal["invalid_input", "store_unavailable", "timeout"]
ErrorAction = Literal["rephrase", "retry", "give_up"]

READ_ERROR_ACTIONS: dict[ErrorClass, ErrorAction] = {
    "invalid_input": "rephrase",
    "store_unavailable": "retry",
    "timeout": "retry",
}


@dataclass(frozen=True)
class ToolHints:
    """MCP tool annotations (spec revision 2026-07-28). The spec defaults
    read as destructive + open-world, so every tool declares explicitly."""

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


READ_ONLY = ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)


@dataclass(frozen=True)
class ToolRegistration:
    name: str
    fn: Callable[..., Any]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    description: str
    surfaces: frozenset[Surface]
    scopes: frozenset[str]
    privileged: bool
    hints: ToolHints
    timeout_s: float
    error_actions: dict[ErrorClass, ErrorAction] = field(
        default_factory=lambda: dict(READ_ERROR_ACTIONS)
    )


_BOTH: frozenset[Surface] = frozenset({"mcp", "http"})
_READ: frozenset[str] = frozenset({"resgraph:read"})

TOOL_REGISTRY: tuple[ToolRegistration, ...] = (
    ToolRegistration(
        name="blast_radius",
        fn=blast_radius,
        input_model=BlastRadiusIn,
        output_model=BlastRadiusOut,
        description=(
            "Everything affected if a resource dies. Returns bare refs "
            "{id, type, one_line} — call fetch_resource for detail on the "
            "few that matter; do not fetch every ref. depth is clamped to "
            "the platform cap (depth_clamped=true in the response). "
            "at=None reads the live graph; at=<ISO-8601 UTC> reconstructs "
            "that moment. If truncated=true, report 'at least total_count' "
            "and follow pagination_hint."
        ),
        surfaces=_BOTH,
        scopes=_READ,
        privileged=False,
        hints=READ_ONLY,
        timeout_s=10.0,
    ),
    ToolRegistration(
        name="dependency_path",
        fn=dependency_path,
        input_model=DependencyPathIn,
        output_model=DependencyPathOut,
        description=(
            "Why does A depend on B: one shortest dependency path in the "
            "live graph, as node ids plus edge types. found=false means no "
            "path exists — that is an answer, not an error."
        ),
        surfaces=_BOTH,
        scopes=_READ,
        privileged=False,
        hints=READ_ONLY,
        timeout_s=10.0,
    ),
    ToolRegistration(
        name="resource_history",
        fn=resource_history,
        input_model=ResourceHistoryIn,
        output_model=ResourceHistoryOut,
        description=(
            "Every recorded change to one resource, oldest first, from the "
            "cold store. Paginated under the response token cap — follow "
            "pagination_hint rather than assuming the page is complete."
        ),
        surfaces=_BOTH,
        scopes=_READ,
        privileged=False,
        hints=READ_ONLY,
        timeout_s=30.0,
    ),
    ToolRegistration(
        name="world_diff",
        fn=world_diff,
        input_model=WorldDiffIn,
        output_model=WorldDiffOut,
        description=(
            "What changed between two moments (ISO-8601 UTC): refs bucketed "
            "created/deleted/changed via one_line. Bracket the window "
            "tightly — do not diff a whole day when the alert gives you ten "
            "minutes. counts carries per-bucket totals even when truncated."
        ),
        surfaces=_BOTH,
        scopes=_READ,
        privileged=False,
        hints=READ_ONLY,
        timeout_s=30.0,
    ),
    ToolRegistration(
        name="fetch_resource",
        fn=fetch_resource,
        input_model=FetchResourceIn,
        output_model=FetchResourceOut,
        description=(
            "Full detail for any resource by id — the fetch half of the "
            "refs+fetch pattern. at=None reads live; at=<ISO-8601 UTC> "
            "reads the reconstructed past. found=false means the resource "
            "does not exist (or was deleted) at that moment."
        ),
        surfaces=_BOTH,
        scopes=_READ,
        privileged=False,
        hints=READ_ONLY,
        timeout_s=5.0,
    ),
)


def registry_entry(name: str) -> ToolRegistration:
    for entry in TOOL_REGISTRY:
        if entry.name == name:
            return entry
    raise KeyError(f"no such tool: {name!r}")
