"""`resgraph cold` — cold-store commands."""

import json

import typer

from resgraph.consumer import DEFAULT_STREAM, StreamConsumer
from resgraph.schema import UpdateMessage

from . import store

app = typer.Typer(help="Cold store: Iceberg history + event-time travel.")


@app.command("init")
def init_cmd() -> None:
    """Create the catalog and tables (idempotent)."""
    store.ensure_tables(store.get_catalog())
    typer.echo("cold store ok")


@app.command("ingest")
def ingest_cmd(
    redis_url: str = typer.Option("redis://localhost:6379"),
    stream: str = typer.Option(DEFAULT_STREAM),
    group: str = typer.Option("resgraph-cold"),
    name: str = typer.Option("c1"),
    max_messages: int = typer.Option(0, help="Stop after N messages (0 = no limit)."),
    exit_on_idle: bool = typer.Option(False, "--exit-on-idle"),
    batch: int = typer.Option(1024, help="Messages per read/append commit."),
) -> None:
    """Consume the update stream into the events table (at-least-once
    appends; readers dedupe). Resumes unacknowledged entries after a crash."""
    catalog = store.get_catalog()
    store.ensure_tables(catalog)

    def apply(msgs: list[UpdateMessage]) -> tuple[int, int]:
        return (store.append_events(catalog, msgs), 0)

    consumer = StreamConsumer(redis_url, apply, stream=stream, group=group, name=name, batch=batch)
    try:
        counters = consumer.run(max_messages=max_messages or None, exit_on_idle=exit_on_idle)
    finally:
        consumer.close()
    typer.echo(json.dumps(counters))
