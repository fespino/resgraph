"""Cold-store benchmark: append rate, as-of latency, storage (D4, D11-D13).

Generation is streamed in batches and NOT counted in append time; the
append clock covers events_to_arrow + the Iceberg commit only. As-of
latency is measured at fractional positions through the history, with
and without snapshot acceleration.

Run: uv run python benchmarks/cold_bench.py [--messages N] [--batch N]
     [--resources N] [--snap-every N] [--skip-queries]
"""

import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

from resgraph.cold import queries, store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World

SEED = 42
BENCH_DIR = Path("/tmp/resgraph-cold-bench")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=1_000_000)
    ap.add_argument("--resources", type=int, default=10_000)
    ap.add_argument("--batch", type=int, default=8192)
    ap.add_argument("--snap-every", type=int, default=200_000)
    ap.add_argument("--skip-queries", action="store_true")
    args = ap.parse_args()

    shutil.rmtree(BENCH_DIR, ignore_errors=True)
    catalog = store.get_catalog(BENCH_DIR)
    store.ensure_tables(catalog)

    churn = Churn(World(SEED, args.resources))
    gen = iter(churn.snapshot())
    produced = 0
    append_s = 0.0
    marks = []  # event_time at fractional positions, for the query phase

    def next_msg():
        nonlocal gen
        try:
            return next(gen)
        except StopIteration:
            gen = None
            return churn.next_message()

    print(f"# appending {args.messages} events, batch {args.batch}", file=sys.stderr)
    while produced < args.messages:
        n = min(args.batch, args.messages - produced)
        batch = [next_msg() if gen else churn.next_message() for _ in range(n)]
        t0 = time.monotonic()
        store.append_events(catalog, batch)
        append_s += time.monotonic() - t0
        produced += n
        for frac in (0.25, 0.5, 0.75, 0.95):
            pos = int(args.messages * frac)
            if produced - n < pos <= produced:
                marks.append((frac, batch[pos - (produced - n) - 1].event_time))

    files = list(BENCH_DIR.rglob("*.parquet"))
    total = sum(f.stat().st_size for f in BENCH_DIR.rglob("*") if f.is_file())
    result = {
        "messages": produced,
        "batch": args.batch,
        "append_s": round(append_s, 2),
        "append_events_per_s": round(produced / append_s),
        "data_mb": round(sum(f.stat().st_size for f in files) / 1024**2, 1),
        "total_mb": round(total / 1024**2, 1),
        "parquet_files": len(files),
    }

    if not args.skip_queries:
        # snapshots at the 25/50/75 marks (deterministic, documented)
        snap_s = 0.0
        n_snaps = 0
        for frac, t in marks:
            if frac in (0.25, 0.5, 0.75):
                t0 = time.monotonic()
                queries.snapshot_at(catalog, t)
                snap_s += time.monotonic() - t0
                n_snaps += 1
        result["snapshots"] = n_snaps
        result["snapshot_s_each"] = round(snap_s / max(n_snaps, 1), 2)

        for frac, t in marks:
            for accel in (True, False):
                times = []
                for _ in range(3):
                    t0 = time.monotonic()
                    rows = queries.state_at(catalog, t, use_snapshots=accel)
                    times.append(time.monotonic() - t0)
                key = f"as_of_p50_s@{int(frac * 100)}pct_{'snap' if accel else 'replay'}"
                result[key] = round(statistics.median(times), 2)
                result.setdefault("resources_at_marks", {})[f"{int(frac * 100)}pct"] = len(rows)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
