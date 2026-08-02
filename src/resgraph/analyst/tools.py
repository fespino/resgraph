"""The registry's third surface: Anthropic tool blocks, executed in-process.

Same derivation discipline as the MCP and HTTP surfaces — TOOL_REGISTRY
is the only source, and this surface additionally refuses anything that
is not read-only: the agent's surface is safe by construction, not by
prompt.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from resgraph.query.executor import QueryContext
from resgraph.tools.context import CallerContext
from resgraph.tools.registry import TOOL_REGISTRY, ToolRegistration


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    payload: str


def _error(error_class: str, message: str, action: str) -> ToolOutcome:
    return ToolOutcome(
        ok=False,
        payload=json.dumps(
            {"error_class": error_class, "message": message, "action": action}, sort_keys=True
        ),
    )


class RegistryToolset:
    def __init__(
        self,
        qctx_factory: Callable[[], QueryContext],
        entries: tuple[ToolRegistration, ...] = TOOL_REGISTRY,
    ) -> None:
        for entry in entries:
            if not entry.hints.read_only or entry.privileged:
                raise RuntimeError(
                    f"tool {entry.name!r} is not a plain read tool; the analyst surface refuses it"
                )
        self._entries = {e.name: e for e in entries}
        self._qctx_factory = qctx_factory

    def blocks(self) -> list[dict[str, Any]]:
        return [
            {
                "name": e.name,
                "description": e.description,
                "input_schema": e.input_model.model_json_schema(),
            }
            for e in self._entries.values()
        ]

    def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        entry = self._entries.get(name)
        if entry is None:
            return _error("invalid_input", f"unknown tool {name!r}", "rephrase")
        try:
            parsed = entry.input_model.model_validate(args)
        except ValidationError as e:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            return _error("invalid_input", details, entry.error_actions["invalid_input"])
        qctx = self._qctx_factory()
        try:
            result = entry.fn(parsed, ctx=CallerContext("analyst", entry.scopes, qctx))
            return ToolOutcome(ok=True, payload=result.model_dump_json())
        except Exception as e:
            return _error("store_unavailable", str(e), entry.error_actions["store_unavailable"])
        finally:
            if qctx.session is not None:
                qctx.session.close()


_driver: Any = None
_catalog: Any = None
_init_lock = threading.Lock()


def _query_context() -> QueryContext:
    def hot_session() -> Any:
        global _driver
        from resgraph.graph import client as hot_client

        with _init_lock:
            if _driver is None:
                _driver = hot_client.get_driver()
        return _driver.session()

    def cold_catalog() -> Any:
        global _catalog
        from resgraph.cold import store as cold_store

        with _init_lock:
            if _catalog is None:
                _catalog = cold_store.get_catalog()
        return _catalog

    return QueryContext(session_factory=hot_session, catalog_factory=cold_catalog)


def default_toolset() -> RegistryToolset:
    return RegistryToolset(_query_context)
