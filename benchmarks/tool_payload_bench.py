"""Tool payload budgets: does the refs+cap shaping keep p100 flat as
the world grows, where a fat response grows with it?

Per world size: seed memgraph (snapshot + churn), then for sampled
roots measure blast_radius through the canonical tool (refs, capped)
against the same traversal serialized fat (full attrs via
with_attrs=True — the route-shaped payload the tool refuses to be).
fetch_resource measures the detail arm; resource_history and
world_diff run against a temp cold catalog fed the same stream.
Token estimate = len(json)/4, the same rule the cap enforces.

Run: uv run python benchmarks/tool_payload_bench.py [--sizes 1000,10000,100000]
"""

import argparse
import json
import shutil
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

from resgraph.cold import store as cold_store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.graph import queries as hot_queries
from resgraph.graph.client import get_driver
from resgraph.graph.ingest import apply_batch
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema, wipe
from resgraph.query.executor import QueryContext
from resgraph.tools.canonical.entity import FetchResourceIn, fetch_resource
from resgraph.tools.canonical.history import (
    ResourceHistoryIn,
    WorldDiffIn,
    resource_history,
    world_diff,
)
from resgraph.tools.canonical.traversal import BlastRadiusIn, blast_radius
from resgraph.tools.context import CallerContext

SEED = 42
COLD_DIR = Path("/tmp/resgraph-tool-bench-cold")
ROOTS_PER_SIZE = 30


def tokens(payload: object) -> int:
    return len(json.dumps(payload, default=str)) // 4


def pcts(xs: list[int]) -> dict[str, int]:
    xs = sorted(xs)
    return {
        "p50": int(statistics.median(xs)),
        "p95": xs[max(0, int(len(xs) * 0.95) - 1)],
        "p100": xs[-1],
    }


def run_size(session, n_resources: int) -> dict[str, object]:
    churn = Churn(World(SEED, n_resources))
    snap = list(churn.snapshot())
    more = [churn.next_message() for _ in range(n_resources)]
    wipe(session)
    init_schema(session)
    load_snapshot(session, snap)
    apply_batch(session, more)

    alive = {m.resource_id: m.op.value for m in [*snap, *more]}
    roots = sorted(rid for rid, op in alive.items() if op == "upsert")
    roots = roots[:: max(1, len(roots) // ROOTS_PER_SIZE)][:ROOTS_PER_SIZE]

    ctx = CallerContext("mcp", frozenset({"resgraph:read"}), QueryContext(session=session))
    shaped: list[int] = []
    fat: list[int] = []
    fetches: list[int] = []
    for root in roots:
        out = blast_radius(BlastRadiusIn(resource_id=root, depth=6), ctx=ctx)
        shaped.append(tokens(out.model_dump()))
        rows = hot_queries.blast_radius(session, root, depth=6, with_attrs=True)
        fat.append(tokens([r.model_dump() for r in rows]))
        detail = fetch_resource(FetchResourceIn(resource_id=root), ctx=ctx)
        fetches.append(tokens(detail.model_dump()))
    return {
        "resources": n_resources,
        "blast_radius_refs": pcts(shaped),
        "blast_radius_fat": pcts(fat),
        "fetch_resource": pcts(fetches),
        "messages": [*snap, *more],
        "roots": roots,
    }


def run_cold(messages, roots) -> dict[str, object]:
    shutil.rmtree(COLD_DIR, ignore_errors=True)
    catalog = cold_store.get_catalog(COLD_DIR)
    cold_store.ensure_tables(catalog)
    cold_store.append_events(catalog, messages)
    ctx = CallerContext("mcp", frozenset({"resgraph:read"}), QueryContext(catalog=catalog))
    histories = [
        tokens(resource_history(ResourceHistoryIn(resource_id=r), ctx=ctx).model_dump())
        for r in roots
    ]
    t2 = messages[-1].event_time
    t1 = messages[len(messages) // 2].event_time
    now = datetime.now(UTC)
    if t2.tzinfo is None:
        t1, t2 = t1.replace(tzinfo=UTC), t2.replace(tzinfo=UTC)
    _ = now
    diff_tokens = tokens(world_diff(WorldDiffIn(from_t=t1, to_t=t2), ctx=ctx).model_dump())
    return {"resource_history": pcts(histories), "world_diff": diff_tokens}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,10000,100000")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    driver = get_driver()
    try:
        driver.verify_connectivity()
    except Exception as e:
        sys.exit(f"stores not reachable: {e}")

    results: list[dict[str, object]] = []
    with driver.session() as session:
        for n in sizes:
            r = run_size(session, n)
            messages, roots = r.pop("messages"), r.pop("roots")
            if n == sizes[len(sizes) // 2]:
                r["cold"] = run_cold(messages, roots[:10])
            results.append(r)
            print(json.dumps(r, default=str))
    driver.close()


if __name__ == "__main__":
    main()
