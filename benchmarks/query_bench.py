"""Query-layer benchmark: composite time split + the residual-filter
delta (D15-D16, D4).

Three world sizes; per-step timing of the composite route (cold
reconstruction vs BFS traverse vs residual filter), explain latency,
and the push-down argument measured directly: the same predicate run
pushed (DuckDB WHERE) vs forced residual (Python), on the /world route
where push-down is a scan reduction.

Run: uv run python benchmarks/query_bench.py [--sizes small,medium,large]
"""

import argparse
import json
import shutil
import statistics
import time
from pathlib import Path

from resgraph.cold import queries as cold_queries
from resgraph.cold import store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.query.dsl import parse_filter
from resgraph.query.executor import QueryContext, _blast_bfs, _residual_filter, execute_plan
from resgraph.query.planner import (
    Query,
    _duckdb_where,
    bind_params,
    composite_projection,
    plan,
    where_cols,
)

SEED = 42
BENCH_DIR = Path("/tmp/resgraph-query-bench")
SIZES = {
    "small": (1_000, 10_000),
    "medium": (5_000, 100_000),
    "large": (10_000, 500_000),
    "xl": (10_000, 1_000_000),
}
REPS = 7


def _p50(samples: list[float]) -> float:
    return statistics.median(samples)


def _build(resources: int, churn_n: int, batch: int = 8192):
    d = BENCH_DIR / f"{resources}-{churn_n}"
    shutil.rmtree(d, ignore_errors=True)
    cat = store.get_catalog(d)
    store.ensure_tables(cat)
    churn = Churn(World(SEED, resources))

    def gen():
        yield from churn.snapshot()
        for _ in range(churn_n):
            yield churn.next_message()

    buf = []
    for m in gen():
        buf.append(m)
        if len(buf) == batch:
            store.append_events(cat, buf)
            buf = []
    if buf:
        store.append_events(cat, buf)
    cold_queries.snapshot_at(cat)
    t_end = cold_queries.latest_event_time(cat)
    return cat, t_end


def _composite_split(cat, t, root, preds, project=True):
    claimable = [p for p in preds]
    where = _duckdb_where(claimable) or None
    t0 = time.perf_counter()
    state = cold_queries.state_at(
        cat,
        t,
        where=where,
        params=bind_params(claimable),
        annotate=True,
        projection=composite_projection(False) if project else None,
        where_cols=where_cols(claimable),
    )
    t1 = time.perf_counter()
    affected = _blast_bfs(state, root, 3)
    t2 = time.perf_counter()
    rows = [
        {"id": r["resource_id"], "type": r["resource_type"]}
        for r in state
        if r["resource_id"] in affected and r["matched"]
    ]
    t3 = time.perf_counter()
    return {
        "reconstruct_s": t1 - t0,
        "traverse_s": t2 - t1,
        "filter_s": t3 - t2,
        "affected": len(rows),
        "world_rows": len(state),
    }


def _hot_bench(resources: int = 5_000, churn_n: int = 20_000) -> dict:
    """Live endpoints end-to-end (TestClient over a real Memgraph): the
    D4 live-p50 budget, measured through the whole API path."""
    from fastapi.testclient import TestClient

    from resgraph.api import app as api_app
    from resgraph.graph.client import get_driver
    from resgraph.graph.ingest import apply_batch
    from resgraph.graph.loader import load_snapshot
    from resgraph.graph.schema import init_schema, wipe

    churn = Churn(World(SEED, resources))
    snap = list(churn.snapshot())
    more = [churn.next_message() for _ in range(churn_n)]
    driver = get_driver()
    with driver.session() as s:
        wipe(s)
        init_schema(s)
        load_snapshot(s, snap)
        apply_batch(s, more)
        client = TestClient(api_app.app)
        api_app.app.dependency_overrides[api_app.get_ctx] = lambda: QueryContext(session=s)
        try:
            last = {}
            for m in snap + more:
                last[m.resource_id] = m.op.value
            roots = sorted(
                rid for rid, op in last.items() if op == "upsert" and rid.startswith("host")
            )[:20]
            blast, single = [], []
            for _ in range(5):
                for root in roots:
                    t0 = time.perf_counter()
                    r = client.get(f"/blast-radius/{root}", params={"filter": "type=vm"})
                    assert r.status_code == 200
                    blast.append(time.perf_counter() - t0)
                    t0 = time.perf_counter()
                    assert client.get(f"/resources/{root}").status_code == 200
                    single.append(time.perf_counter() - t0)
        finally:
            api_app.app.dependency_overrides.clear()
    driver.close()
    return {
        "live_blast_radius_p50_ms": round(_p50(blast) * 1e3, 2),
        "live_resource_p50_ms": round(_p50(single) * 1e3, 2),
        "requests": len(blast) + len(single),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="small,medium,large")
    ap.add_argument("--hot", action="store_true", help="also bench live endpoints (needs memgraph)")
    args = ap.parse_args()

    if args.hot:
        print("== hot: live endpoints over Memgraph ==")
        print(json.dumps(_hot_bench(), indent=2))

    for name in args.sizes.split(","):
        resources, churn_n = SIZES[name]
        print(f"== {name}: {resources} resources, ~{churn_n} churn events ==")
        cat, t_end = _build(resources, churn_n)
        state = cold_queries.state_at(cat, t_end)
        root = next(r["resource_id"] for r in state if r["resource_id"].startswith("host"))

        preds = parse_filter("type=vm")
        splits = [_composite_split(cat, t_end, root, preds) for _ in range(REPS)]
        full = [_composite_split(cat, t_end, root, preds, project=False) for _ in range(REPS)]
        result = {
            "composite_p50_s": round(
                _p50([s["reconstruct_s"] + s["traverse_s"] + s["filter_s"] for s in splits]), 4
            ),
            "reconstruct_p50_s": round(_p50([s["reconstruct_s"] for s in splits]), 4),
            "reconstruct_unprojected_p50_s": round(_p50([s["reconstruct_s"] for s in full]), 4),
            "traverse_p50_s": round(_p50([s["traverse_s"] for s in splits]), 4),
            "world_rows": splits[0]["world_rows"],
            "affected": splits[0]["affected"],
        }

        # push-down vs forced residual, /world route (scan reduction)
        pushed_pred = parse_filter("attrs.zone=z1")
        q = Query("world", at=t_end, predicates=pushed_pred)
        pushed = []
        for _ in range(REPS):
            t0 = time.perf_counter()
            execute_plan(plan(q), QueryContext(catalog=cat))
            pushed.append(time.perf_counter() - t0)
        residual = []
        for _ in range(REPS):
            t0 = time.perf_counter()
            rows = cold_queries.state_at(cat, t_end)
            rows = [
                {"id": r["resource_id"], "type": r["resource_type"], "attrs": r["attrs"]}
                for r in rows
            ]
            _residual_filter(rows, pushed_pred)
            residual.append(time.perf_counter() - t0)
        result["world_pushed_p50_s"] = round(_p50(pushed), 4)
        result["world_residual_p50_s"] = round(_p50(residual), 4)

        expl = []
        for _ in range(50):
            t0 = time.perf_counter()
            plan(
                Query(
                    "blast_radius",
                    root=root,
                    at=t_end,
                    predicates=parse_filter("attrs.zone=z1 AND attrs.frobnitz=9"),
                )
            ).explain()
            expl.append(time.perf_counter() - t0)
        result["explain_p50_ms"] = round(_p50(expl) * 1e3, 3)

        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
