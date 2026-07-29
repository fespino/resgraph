"""resgraph-gen — CLI entry point."""

import time
from itertools import islice

import typer

from .churn import Churn
from .sinks import RedisSink, StdoutSink
from .world import World

app = typer.Typer(help="Deterministic world generator (SPEC D5-D7).", add_completion=False)


def _sink(kind: str, redis_url: str):
    if kind == "redis":
        return RedisSink(redis_url)
    if kind == "stdout":
        return StdoutSink()
    raise typer.BadParameter(f"unknown sink: {kind}")


@app.command()
def seed(
    seed: int = 42,
    resources: int = 10_000,
    sink: str = "stdout",
    redis_url: str = "redis://localhost:6379",
    batch: int = 500,
) -> None:
    """Emit a snapshot of the seeded world (one upsert per resource)."""
    out = _sink(sink, redis_url)
    snapshot = Churn(World(seed, resources)).snapshot()
    while chunk := list(islice(snapshot, batch)):
        out.emit_many(chunk)
    out.close()


@app.command()
def run(
    seed: int = 42,
    resources: int = 10_000,
    count: int = 0,
    duration: float = 0.0,
    rate: float = 0.0,
    sink: str = "stdout",
    redis_url: str = "redis://localhost:6379",
    batch: int = 500,
    mean_interval_us: float = 100.0,
    burst_every: float = 0.0,
    burst_len: float = 0.0,
    burst_x: float = 1.0,
) -> None:
    """Emit a churn stream. count=0 with duration=0 runs forever; rate=0 is
    unthrottled. Throttling is operational only — stream content is
    identical at any rate (D6)."""
    out = _sink(sink, redis_url)
    churn = Churn(World(seed, resources), mean_interval_us, burst_every, burst_len, burst_x)
    emitted = 0
    start = time.monotonic()
    try:
        while True:
            n = batch if count == 0 else min(batch, count - emitted)
            if n <= 0:
                break
            out.emit_many([churn.next_message() for _ in range(n)])
            emitted += n
            elapsed = time.monotonic() - start
            if duration > 0 and elapsed >= duration:
                break
            if rate > 0:
                ahead = emitted / rate - elapsed
                if ahead > 0:
                    time.sleep(ahead)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        out.close()
