"""Traversal benchmark: Memgraph BFS vs Postgres recursive CTE (D1, D4).

Identical seeded graphs in both stores; hub + leaf targets; p50/p95
across targets x runs. Run: uv run python benchmarks/traversal_bench.py
(requires: docker compose up -d memgraph; --profile bench up -d postgres)
"""

import statistics
import sys
import time
from collections import defaultdict
from random import Random

import psycopg

from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.graph.client import get_driver
from resgraph.graph.loader import load_snapshot
from resgraph.graph.queries import DEP_EDGES
from resgraph.graph.schema import init_schema

PG = "postgresql://postgres:resgraph@localhost:5433/postgres"
SEED, RUNS, N_TARGETS, DEPTHS = 42, 5, 20, (3, 5)

CTE = """
WITH RECURSIVE br(src, depth) AS (
  SELECT src, 1 FROM edges WHERE dst = %s
  UNION
  SELECT e.src, br.depth + 1 FROM edges e JOIN br ON e.dst = br.src
  WHERE br.depth < %s
)
SELECT count(DISTINCT src) FROM br
"""


def pick_targets(w: World) -> list[str]:
    rdeps: dict[str, int] = defaultdict(int)
    for rid in w.alive_ids():
        for rel in w.resources[rid].relationships:
            rdeps[rel.target_id] += 1
    hubs = sorted(rdeps, key=lambda k: (-rdeps[k], k))[: N_TARGETS // 2]
    pool = [r for r in w.alive_ids() if r not in set(hubs)]
    leaves = sorted(Random(0).sample(pool, N_TARGETS // 2))
    return hubs + leaves


def load_stores(size: int) -> tuple[World, list[str]]:
    w = World(SEED, size)
    print(f"# world {size}: {len(w.alive_ids())} resources", file=sys.stderr)
    d = get_driver()
    with d.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
        init_schema(s)
        counts = load_snapshot(s, Churn(w).snapshot())
    d.close()
    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS edges")
        conn.execute("CREATE TABLE edges (src text, dst text, type text)")
        with conn.cursor().copy("COPY edges (src, dst, type) FROM STDIN") as cp:
            for rid in w.alive_ids():
                for rel in w.resources[rid].relationships:
                    cp.write_row((rid, rel.target_id, rel.type))
        conn.execute("CREATE INDEX ON edges (dst)")
        conn.execute("ANALYZE edges")
    print(f"# loaded both stores: {counts}", file=sys.stderr)
    return w, pick_targets(w)


def bench(fn, targets: list[str], depth: int) -> tuple[float, float]:
    lat: list[float] = []
    for t in targets:
        for _ in range(RUNS):
            t0 = time.monotonic()
            fn(t, depth)
            lat.append((time.monotonic() - t0) * 1e3)
    return statistics.median(lat), statistics.quantiles(lat, n=20)[18]  # p50, p95


def main(sizes: list[int]) -> None:
    print("| world | store | depth | p50 ms | p95 ms |")
    print("|---|---|---|---|---|")
    for size in sizes:
        _, targets = load_stores(size)
        d = get_driver()
        mg = d.session()

        def mg_query(t: str, depth: int, _s=mg) -> None:
            label = t.split("-", 1)[0]  # anchor label hits the per-label index (D8)
            _s.run(
                f"MATCH (x:{label} {{id: $id}})<-[{DEP_EDGES} *BFS 1..{depth}]-(dep) "
                "WHERE NOT coalesce(dep.deleted, false) RETURN count(DISTINCT dep)",
                id=t,
            ).consume()

        pg = psycopg.connect(PG)

        def pg_query(t: str, depth: int, _c=pg) -> None:
            _c.execute(CTE, (t, depth)).fetchall()

        for depth in DEPTHS:
            for store, fn in (("memgraph", mg_query), ("postgres-cte", pg_query)):
                p50, p95 = bench(fn, targets, depth)
                print(f"| {size:,} | {store} | {depth} | {p50:.1f} | {p95:.1f} |", flush=True)
        mg.close()
        d.close()
        pg.close()


if __name__ == "__main__":
    main([int(x) for x in (sys.argv[1:] or ["10000", "100000"])])
