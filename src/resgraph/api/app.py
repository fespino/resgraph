"""The D15 API surface: fixed endpoints, result budgets, labeled sources.

Every response says which store answered (`source`) and when
(`fetched_at`); every list is capped at MAX_ROWS with `truncated` +
`total_count`. `?explain=true` returns the plan without touching a
store — laziness as an observable property, not a claim.
"""

import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from resgraph import obs
from resgraph.cold import queries as cold_queries
from resgraph.cold import store as cold_store
from resgraph.graph import client as hot_client
from resgraph.graph.ingest import read_node
from resgraph.query.dsl import parse_filter
from resgraph.query.executor import QueryContext, execute_plan
from resgraph.query.planner import Query as PlannerQuery
from resgraph.query.planner import plan as make_plan
from resgraph.tools.http import mount_tools

log = logging.getLogger("resgraph.api")

MAX_ROWS = 1000

Source = Literal["hot", "cold", "composite"]


class Envelope(BaseModel):
    fetched_at: datetime
    source: Source


class ResourceOut(Envelope):
    id: str
    type: str
    phantom: bool
    attrs: dict[str, Any]
    relationships: list[dict[str, str]]


class AffectedOut(BaseModel):
    id: str
    type: str


class BlastRadiusApiOut(Envelope):
    root: str
    depth: int
    at: datetime | None
    affected: list[AffectedOut]
    truncated: bool
    total_count: int


class WorldOut(Envelope):
    at: datetime
    resources: list[dict[str, Any]]
    truncated: bool
    total_count: int


class HistoryOut(Envelope):
    id: str
    events: list[dict[str, Any]]
    truncated: bool
    total_count: int


class DiffOut(Envelope):
    from_t: datetime
    to_t: datetime
    created: list[str]
    deleted: list[str]
    changed: list[str]
    truncated: bool
    total_count: dict[str, int]


def create_app() -> FastAPI:
    app = FastAPI(title="resgraph", version="0.1.0")
    app.state.driver = None
    app.state.catalog = None
    obs.init_metrics()
    app.mount("/metrics", make_asgi_app())

    @app.middleware("http")  # registration is the use; pyright can't see it
    async def telemetry(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ):
        t = time.monotonic()
        response = await call_next(request)
        if request.url.path.startswith("/metrics"):
            return response
        dur = time.monotonic() - t
        route = request.scope.get("route")
        route_path = route.path if route else "unmatched"
        source = getattr(request.state, "source", "other")
        obs.API_SECONDS.record(dur, {"route": route_path, "source": source})
        obs.get_sink("api").emit(
            "api_request",
            route=route_path,
            query=str(request.url.query),
            status=response.status_code,
            ms=round(dur * 1e3, 2),
            source=source,
            **getattr(request.state, "telemetry", {}),
        )
        return response

    return app


app = create_app()


_init_lock = threading.Lock()


def get_ctx(request: Request):
    st = request.app.state

    def hot_session():
        # sync endpoints run in a threadpool: two concurrent first
        # requests must not race two drivers into existence (#80,
        # same class as the MCP server's init lock)
        with _init_lock:
            if st.driver is None:
                st.driver = hot_client.get_driver()
        return st.driver.session()

    def cold_catalog():
        with _init_lock:
            if st.catalog is None:
                st.catalog = cold_store.get_catalog()
        return st.catalog

    ctx = QueryContext(session_factory=hot_session, catalog_factory=cold_catalog)
    try:
        yield ctx
    finally:
        if ctx.session is not None:
            ctx.session.close()


Ctx = Annotated[QueryContext, Depends(get_ctx)]

mount_tools(app, get_ctx)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(filter_: str | None, at: str | None):
    try:
        preds = parse_filter(filter_)
        at_t = datetime.fromisoformat(at) if at else None
        if at_t is not None and at_t.tzinfo is None:
            raise ValueError("at must be timezone-aware ISO-8601")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return preds, at_t


def _cap[T](rows: list[T]) -> tuple[list[T], bool, int]:
    return rows[:MAX_ROWS], len(rows) > MAX_ROWS, len(rows)


@app.get("/resources/{resource_id}", response_model=ResourceOut)
def resource(request: Request, resource_id: str, ctx: Ctx, explain: bool = False):
    request.state.source = "plan" if explain else "hot"
    if explain:
        return _explain_response(
            [{"store": "hot", "op": "cypher", "detail": f"read_node({resource_id!r})"}]
        )
    try:
        node = read_node(ctx.require("hot"), resource_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if node is None or node["deleted"]:
        raise HTTPException(status_code=404, detail="no such resource")
    return ResourceOut(
        fetched_at=_now(),
        source="hot",
        id=node["id"],
        type=resource_id.split("-", 1)[0],
        phantom=node["phantom"],
        attrs=node["attrs"],
        relationships=[{"type": t.lower(), "target_id": tid} for t, tid in node["rels"]],
    )


@app.get("/blast-radius/{resource_id}")
def blast_radius(
    request: Request,
    resource_id: str,
    ctx: Ctx,
    depth: Annotated[int, Query(ge=1)] = 3,
    filter: str | None = None,
    at: str | None = None,
    explain: bool = False,
):
    preds, at_t = _parse(filter, at)
    request.state.source = "plan" if explain else ("hot" if at_t is None else "composite")
    request.state.telemetry = {"root": resource_id, "depth": depth, "at": at}
    try:
        p = make_plan(
            PlannerQuery("blast_radius", root=resource_id, depth=depth, at=at_t, predicates=preds)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if explain:
        return _explain_response(p.explain())
    log.debug("plan %s", json.dumps(p.explain()))
    try:
        rows = execute_plan(p, ctx)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    rows, truncated, total = _cap(rows)
    request.state.telemetry.update(total=total, truncated=truncated)
    return BlastRadiusApiOut(
        fetched_at=_now(),
        source="hot" if at_t is None else "composite",
        root=resource_id,
        depth=depth,
        at=at_t,
        affected=[AffectedOut(**r) for r in rows],
        truncated=truncated,
        total_count=total,
    )


@app.get("/world")
def world(
    request: Request,
    at: str,
    ctx: Ctx,
    filter: str | None = None,
    explain: bool = False,
):
    preds, at_t = _parse(filter, at)
    if at_t is None:
        raise HTTPException(status_code=400, detail="at must be a non-empty ISO-8601 timestamp")
    request.state.source = "plan" if explain else "cold"
    request.state.telemetry = {"at": at}
    try:
        p = make_plan(PlannerQuery("world", at=at_t, predicates=preds))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if explain:
        return _explain_response(p.explain())
    rows = execute_plan(p, ctx)
    rows, truncated, total = _cap(rows)
    return WorldOut(
        fetched_at=_now(),
        source="cold",
        at=at_t,
        resources=rows,
        truncated=truncated,
        total_count=total,
    )


@app.get("/history/{resource_id}")
def history(request: Request, resource_id: str, ctx: Ctx, explain: bool = False):
    request.state.source = "plan" if explain else "cold"
    if explain:
        return _explain_response(
            [{"store": "cold", "op": "duckdb", "detail": f"history({resource_id!r})"}]
        )
    rows = cold_queries.history(ctx.require("cold"), resource_id, limit=MAX_ROWS + 1)
    rows, truncated, total = _cap(rows)
    return HistoryOut(
        fetched_at=_now(),
        source="cold",
        id=resource_id,
        events=rows,
        truncated=truncated,
        total_count=total,
    )


@app.get("/diff")
def diff(
    request: Request,
    from_t: Annotated[str, Query(alias="from")],
    to_t: Annotated[str, Query(alias="to")],
    ctx: Ctx,
    explain: bool = False,
):
    _, t1 = _parse(None, from_t)
    _, t2 = _parse(None, to_t)
    if t1 is None or t2 is None:
        raise HTTPException(status_code=400, detail="from/to must be non-empty ISO-8601 timestamps")
    request.state.source = "plan" if explain else "cold"
    if explain:
        return _explain_response(
            [
                {
                    "store": "cold",
                    "op": "duckdb",
                    "detail": f"diff(state_at({t1.isoformat()}), state_at({t2.isoformat()}))",
                }
            ]
        )
    d = cold_queries.diff(ctx.require("cold"), t1, t2)
    totals = {k: len(v) for k, v in d.items()}
    return DiffOut(
        fetched_at=_now(),
        source="cold",
        from_t=t1,
        to_t=t2,
        created=d["created"][:MAX_ROWS],
        deleted=d["deleted"][:MAX_ROWS],
        changed=d["changed"][:MAX_ROWS],
        truncated=any(v > MAX_ROWS for v in totals.values()),
        total_count=totals,
    )


def _explain_response(plan_repr: dict[str, Any] | list[dict[str, str]]) -> dict[str, Any]:
    return {
        "fetched_at": _now().isoformat(),
        "source": "plan",
        "plan": plan_repr if isinstance(plan_repr, dict) else {"steps": plan_repr, "residual": []},
    }
