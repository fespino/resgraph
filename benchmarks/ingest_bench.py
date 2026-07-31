"""Ingest benchmark: stream -> consumer -> hot store throughput + RSS (D4).

Publishes a seeded snapshot+churn stream to Redis (streaming batches, so
publishing never inflates this process's RSS), then times the consume
phase. The consumer runs as the real `resgraph ingest` CLI in a child
process: elapsed wall time gives updates/s, and the child's peak RSS
(getrusage RUSAGE_CHILDREN) gives the D4 memory number without the
publisher's footprint polluting it.

Run: uv run python benchmarks/ingest_bench.py [--messages N]
     [--resources N] [--profile]
(requires: docker compose up -d memgraph redis)

--profile consumes in-process under cProfile instead (no rate/RSS
reported from that mode; profiles exist to locate cost, not to publish).
"""

import argparse
import cProfile
import json
import pstats
import resource
import subprocess
import sys
import time
from itertools import islice

from resgraph.gen.churn import Churn
from resgraph.gen.sinks import RedisSink
from resgraph.gen.world import World
from resgraph.graph.client import get_driver
from resgraph.graph.consumer import Consumer
from resgraph.graph.schema import init_schema

REDIS_URL = "redis://localhost:6379"
STREAM = "resgraph:bench:ingest"
SEED = 42


def publish(resources: int, messages: int) -> int:
    """Snapshot + churn to the bench stream, streamed in batches."""
    import redis

    r = redis.Redis.from_url(REDIS_URL)
    r.delete(STREAM)
    r.close()
    sink = RedisSink(REDIS_URL, stream=STREAM, maxlen=messages + 1000)
    churn = Churn(World(SEED, resources))
    n = 0
    snapshot = churn.snapshot()
    while chunk := list(islice(snapshot, 1000)):
        sink.emit_many(chunk)
        n += len(chunk)
    while n < messages:
        batch = [churn.next_message() for _ in range(min(1000, messages - n))]
        sink.emit_many(batch)
        n += len(batch)
    sink.close()
    return n


def wipe_store() -> None:
    d = get_driver()
    with d.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
        init_schema(s)
    d.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resources", type=int, default=10_000)
    ap.add_argument("--messages", type=int, default=50_000)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    print(f"# publishing {args.messages} messages (world {args.resources})", file=sys.stderr)
    n = publish(args.resources, args.messages)
    wipe_store()
    print(f"# published {n}; consuming", file=sys.stderr)

    if args.profile:
        d = get_driver()
        with d.session() as s:
            consumer = Consumer(
                REDIS_URL, s, stream=STREAM, group="bench-prof", name="c1", batch=args.batch
            )
            prof = cProfile.Profile()
            prof.enable()
            counters = consumer.run(max_messages=n, exit_on_idle=True)
            prof.disable()
            consumer.close()
        d.close()
        pstats.Stats(prof).sort_stats("cumulative").print_stats(25)
        print(json.dumps(counters))
        return

    t0 = time.monotonic()
    proc = subprocess.run(
        [
            "uv",
            "run",
            "resgraph",
            "ingest",
            "--stream",
            STREAM,
            "--group",
            f"bench-{int(t0)}",
            "--max-messages",
            str(n),
            "--batch",
            str(args.batch),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed = time.monotonic() - t0
    counters = json.loads(proc.stdout)
    # macOS reports ru_maxrss in bytes; Linux in KiB
    maxrss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    rss_mb = maxrss / (1024**2) if sys.platform == "darwin" else maxrss / 1024
    print(
        json.dumps(
            {
                "messages": n,
                "seconds": round(elapsed, 2),
                "updates_per_s": round(n / elapsed),
                "consumer_peak_rss_mb": round(rss_mb, 1),
                **counters,
            }
        )
    )


if __name__ == "__main__":
    main()
