"""resgraph-gen — CLI entry point."""

import json
import time
from itertools import islice
from typing import Annotated

import typer

from .churn import Churn
from .scenarios import ScenarioType, generate_scenario, generate_set
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


@app.command("final-state")
def final_state(
    seed: int = 42,
    resources: int = 10_000,
    count: int = 0,
) -> None:
    """Replay the deterministic stream and print the final alive state
    as JSONL — the oracle `resgraph reconcile --oracle` compares
    against (D6 makes this exact, not statistical)."""
    from resgraph.schema import Op

    churn = Churn(World(seed, resources))
    last = {}
    for m in churn.snapshot():
        last[m.resource_id] = m
    for _ in range(count):
        m = churn.next_message()
        last[m.resource_id] = m
    for rid in sorted(last):
        m = last[rid]
        if m.op is Op.UPSERT:
            print(
                json.dumps(
                    {
                        "resource_id": rid,
                        "resource_type": m.resource_type.value,
                        "sequence": m.sequence,
                        "attrs": dict(m.attrs),
                        "relationships": [
                            {"type": r.type, "target_id": r.target_id} for r in m.relationships
                        ],
                    }
                )
            )


@app.command()
def scenario(
    scenario_type: Annotated[ScenarioType, typer.Option("--type")],
    seed: int = 42,
    resources: int = 60,
    depth: int = 0,
    messages: str = "",
) -> None:
    """Plant one causal scenario: spec JSON (with ground truth) on
    stdout; --messages FILE also writes the message stream as JSONL.
    depth=0 uses the type's default."""
    generated = generate_scenario(scenario_type, seed, resources, depth=depth or None)
    if messages:
        with open(messages, "w") as f:
            for m in generated.messages:
                f.write(m.model_dump_json() + "\n")
    print(generated.spec.model_dump_json())


@app.command("scenario-set")
def scenario_set(
    seed: int = 42,
    count: int = 30,
    resources: int = 60,
    out: str = "-",
) -> None:
    """Emit a taxonomy-covering scenario set as JSONL (~20% controls)."""
    lines = [g.spec.model_dump_json() for g in generate_set(seed, count, resources)]
    if out == "-":
        print("\n".join(lines))
    else:
        with open(out, "w") as f:
            f.write("\n".join(lines) + "\n")


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
        return
    finally:
        out.close()
