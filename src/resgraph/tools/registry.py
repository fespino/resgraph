"""TOOL_REGISTRY — the single grep-able source of truth for the tool
surface. Both transports derive from it; the drift guard asserts
nothing exists outside it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from resgraph.analyst.executor import (
    ApplyRemediationIn,
    ApplyRemediationOut,
    apply_remediation,
)
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
# idempotent because the D3 watermark makes a replayed message a no-op,
# not because the effect is harmless
PRIVILEGED_WRITE = ToolHints(read_only=False, destructive=True, idempotent=True, open_world=False)


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
# no surface: both transports derive by membership, so an empty set is
# how "external clients have no bypassing use case" is enforced rather
# than promised (D19 admission rule)
_NO_SURFACE: frozenset[Surface] = frozenset()
_WRITE: frozenset[str] = frozenset({"resgraph:write"})

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
    ToolRegistration(
        name="apply_remediation",
        fn=apply_remediation,
        input_model=ApplyRemediationIn,
        output_model=ApplyRemediationOut,
        description=(
            "Execute an approved remediation plan: each step becomes a D2 "
            "update on the ingest stream, confirmed against the watermark, "
            "with reverse-order rollback on failure. Operator-only, after "
            "the typed approval gate — never callable from an agent loop."
        ),
        surfaces=_NO_SURFACE,
        scopes=_WRITE,
        privileged=True,
        hints=PRIVILEGED_WRITE,
        timeout_s=120.0,
    ),
)
