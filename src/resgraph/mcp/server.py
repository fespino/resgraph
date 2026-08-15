"""The MCP surface: a registry loop plus middleware, nothing else.

Targets MCP spec revision 2026-07-28. All five tools are single-shot
reads — no session state, no handles — which is the stateless-
compatible shape that revision asks for.
"""

import threading
from collections.abc import Callable
from inspect import Parameter, Signature
from pathlib import Path
from typing import Any

from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts import Prompt
from mcp.server.mcpserver.resources import TextResource
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from resgraph.cold import store as cold_store
from resgraph.graph import client as hot_client
from resgraph.mcp.skills import Skill, load_skills
from resgraph.query.executor import QueryContext
from resgraph.tools.context import CallerContext
from resgraph.tools.registry import TOOL_REGISTRY, ToolRegistration

SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"
CARD_PATH = Path(__file__).with_name("CARD.md")

# the catalog only changes when the process does: an hour bounds
# cross-restart staleness; "public" because no tool varies by caller
_CATALOG_HINT = CacheHint(ttl_ms=3_600_000, scope="public")
_CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": _CATALOG_HINT,
    "prompts/list": _CATALOG_HINT,
    "resources/list": _CATALOG_HINT,
    "resources/read": _CATALOG_HINT,
    "resources/templates/list": _CATALOG_HINT,
    "server/discover": _CATALOG_HINT,
}

_driver: Any = None
_catalog: Any = None
# parallel tool calls are the intended usage; the lazy init must not
# race two drivers into existence on the first concurrent pair
_init_lock = threading.Lock()


def _query_context() -> QueryContext:
    def hot_session() -> Any:
        global _driver
        with _init_lock:
            if _driver is None:
                _driver = hot_client.get_driver()
        return _driver.session()

    def cold_catalog() -> Any:
        global _catalog
        with _init_lock:
            if _catalog is None:
                _catalog = cold_store.get_catalog()
        return _catalog

    return QueryContext(session_factory=hot_session, catalog_factory=cold_catalog)


def _adapter(entry: ToolRegistration) -> Callable[..., BaseModel]:
    def call(**kwargs: object) -> BaseModel:
        args = entry.input_model.model_validate(kwargs)
        qctx = _query_context()
        try:
            result: BaseModel = entry.fn(args, ctx=CallerContext("mcp", entry.scopes, qctx))
            return result
        finally:
            if qctx.session is not None:
                qctx.session.close()

    params = [
        Parameter(
            name,
            Parameter.KEYWORD_ONLY,
            annotation=f.annotation,
            default=Parameter.empty if f.is_required() else f.default,
        )
        for name, f in entry.input_model.model_fields.items()
    ]
    call.__name__ = entry.name
    call.__doc__ = entry.description
    call.__signature__ = Signature(params, return_annotation=entry.output_model)  # pyright: ignore[reportFunctionMemberAccess]
    call.__annotations__ = {
        name: f.annotation for name, f in entry.input_model.model_fields.items()
    } | {"return": entry.output_model}
    return call


def _skill_prompt(skill: Skill) -> Prompt:
    def render() -> str:
        return skill.body

    return Prompt.from_function(
        render, name=skill.manifest.name, description=skill.manifest.description
    )


def build_server(skills_dir: Path = SKILLS_DIR) -> MCPServer:
    server = MCPServer(
        "resgraph",
        version="0.1.0",
        instructions=(
            "Read-only investigation tools over a resource graph with full "
            "history. Traversals return bare refs — fetch_resource for "
            "detail on the few that matter. Read the resgraph://card "
            "resource before first use."
        ),
        cache_hints=_CACHE_HINTS,
    )
    for entry in TOOL_REGISTRY:
        if "mcp" in entry.surfaces:
            server.add_tool(
                _adapter(entry),
                name=entry.name,
                description=entry.description,
                annotations=ToolAnnotations(
                    read_only_hint=entry.hints.read_only,
                    destructive_hint=entry.hints.destructive,
                    idempotent_hint=entry.hints.idempotent,
                    open_world_hint=entry.hints.open_world,
                ),
                meta={
                    "timeout_s": entry.timeout_s,
                    "error_actions": dict(entry.error_actions),
                    "scopes": sorted(entry.scopes),
                },
            )
    for skill in load_skills(skills_dir):
        server.add_prompt(_skill_prompt(skill))
    server.add_resource(
        TextResource(
            uri="resgraph://card",
            name="server-card",
            description="What this server is, its tools, budgets, and limits.",
            mime_type="text/markdown",
            text=CARD_PATH.read_text(),
        )
    )
    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
