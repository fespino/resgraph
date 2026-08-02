"""The HTTP projection of the registry: POST /tools/{name}.

The transport is never the truth-bearing module — this file only loops
over the registry; the drift guard rejects any /tools route born
anywhere else.
"""

from collections.abc import Callable
from inspect import Parameter, Signature
from typing import Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from resgraph.query.executor import QueryContext
from resgraph.tools.context import CallerContext
from resgraph.tools.registry import TOOL_REGISTRY, ToolRegistration


def _handler(entry: ToolRegistration, ctx_dep: Callable[..., Any]) -> Callable[..., BaseModel]:
    def call(body: Any, qctx: Any) -> BaseModel:
        result: BaseModel = entry.fn(body, ctx=CallerContext("http", entry.scopes, qctx))
        return result

    call.__name__ = entry.name
    call.__doc__ = entry.description
    call.__signature__ = Signature(  # pyright: ignore[reportFunctionMemberAccess]
        [
            Parameter("body", Parameter.POSITIONAL_OR_KEYWORD, annotation=entry.input_model),
            Parameter(
                "qctx",
                Parameter.POSITIONAL_OR_KEYWORD,
                annotation=QueryContext,
                default=Depends(ctx_dep),
            ),
        ],
        return_annotation=entry.output_model,
    )
    call.__annotations__ = {
        "body": entry.input_model,
        "qctx": QueryContext,
        "return": entry.output_model,
    }
    return call


def mount_tools(app: FastAPI, ctx_dep: Callable[..., Any]) -> None:
    for entry in TOOL_REGISTRY:
        if "http" in entry.surfaces:
            app.add_api_route(
                f"/tools/{entry.name}",
                _handler(entry, ctx_dep),
                methods=["POST"],
                response_model=entry.output_model,
                tags=["tools"],
            )
