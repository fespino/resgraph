"""The D15 API surface: fixed endpoints, result budgets, labeled sources.

Every response says which store answered (`source`) and when
(`fetched_at`); every list is capped at MAX_ROWS with `truncated` +
`total_count`. `?explain=true` returns the plan without touching a
store — laziness as an observable property, not a claim.
"""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from resgraph.cold import queries as cold_queries
from resgraph.cold import store as cold_store
from resgraph.graph import client as hot_client
from resgraph.graph.ingest import read_node
from resgraph.query.dsl import parse_filter
from resgraph.query.executor import QueryContext
from resgraph.query.planner import Query as PlannerQuery
from resgraph.query.planner import plan as make_plan

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
    attrs: dict
    relationships: list[dict]


class AffectedOut(BaseModel):
    id: str
    type: str


class BlastRadiusOut(Envelope):
    root: str
    depth: int
    at: datetime | None
    affected: list[AffectedOut]
    truncated: bool
    total_count: int


class WorldOut(Envelope):
    at: datetime
    resources: list[dict]
    truncated: bool
    total_count: int


class HistoryOut(Envelope):
    id: str
    events: list[dict]
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

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        t = time.monotonic()
        response = await call_next(request)
        log.info(
            json.dumps(
                {
                    "route": request.url.path,
                    "query": str(request.url.query),
                    "status": response.status_code,
                    "ms": round((time.monotonic() - t) * 1e3, 1),
                }
            )
        )
        return response

    return app


app = create_app()


def get_ctx(request: Request):
    st = request.app.state

    def hot_session():
        if st.driver is None:
            st.driver = hot_client.get_driver()
        return st.driver.session()

    def cold_catalog():
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


def _cap(rows: list) -> tuple[list, bool, int]:
    return rows[:MAX_ROWS], len(rows) > MAX_ROWS, len(rows)


@app.get("/resources/{resource_id}", response_model=ResourceOut)
def resource(resource_id: str, explain: bool = False, ctx: Ctx = None):
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
    resource_id: str,
    depth: Annotated[int, Query(ge=1)] = 3,
    filter: str | None = None,
    at: str | None = None,
    explain: bool = False,
    ctx: Ctx = None,
):
    preds, at_t = _parse(filter, at)
    try:
        p = make_plan(
            PlannerQuery(
                "blast_radius", root=resource_id, depth=depth, at=at_t, predicates=preds
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if explain:
        return _explain_response(p.explain())
    log.debug("plan %s", json.dumps(p.explain()))
    try:
        rows = p.execute(ctx)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    rows, truncated, total = _cap(rows)
    return BlastRadiusOut(
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
    at: str,
    filter: str | None = None,
    explain: bool = False,
    ctx: Ctx = None,
):
    preds, at_t = _parse(filter, at)
    try:
        p = make_plan(PlannerQuery("world", at=at_t, predicates=preds))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if explain:
        return _explain_response(p.explain())
    rows = p.execute(ctx)
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
def history(resource_id: str, explain: bool = False, ctx: Ctx = None):
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
    from_t: Annotated[str, Query(alias="from")],
    to_t: Annotated[str, Query(alias="to")],
    explain: bool = False,
    ctx: Ctx = None,
):
    _, t1 = _parse(None, from_t)
    _, t2 = _parse(None, to_t)
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


def _explain_response(plan_repr) -> dict:
    return {
        "fetched_at": _now().isoformat(),
        "source": "plan",
        "plan": plan_repr if isinstance(plan_repr, dict) else {"steps": plan_repr, "residual": []},
    }
