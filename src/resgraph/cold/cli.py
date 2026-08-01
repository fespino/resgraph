"""`resgraph cold` — cold-store commands."""

import json
from datetime import datetime

import typer

from resgraph.consumer import DEFAULT_STREAM, StreamConsumer
from resgraph.schema import UpdateMessage

from . import queries, store

app = typer.Typer(help="Cold store: Iceberg history + event-time travel.")


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
    metrics_port: int = typer.Option(0, help="Serve OTel->Prometheus metrics (0 = off)."),
) -> None:
    """Consume the update stream into the events table (at-least-once
    appends; readers dedupe). Resumes unacknowledged entries after a crash."""
    if metrics_port:
        from resgraph import obs

        obs.init_metrics(metrics_port)
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


@app.command("snapshot")
def snapshot_cmd(
    at: str = typer.Option(None, help="Event time (ISO); defaults to the newest event."),
) -> None:
    """Materialize world state into the snapshots table."""
    catalog = store.get_catalog()
    typer.echo(json.dumps(queries.snapshot_at(catalog, _t(at) if at else None)))


@app.command("as-of")
def as_of_cmd(
    at: str = typer.Option(..., help="Event time (ISO)."),
    summary: bool = typer.Option(False, "--summary", help="Counts per type instead of rows."),
    no_snapshots: bool = typer.Option(False, "--no-snapshots", help="Pure event replay."),
) -> None:
    """World state as of an event time."""
    rows = queries.state_at(store.get_catalog(), _t(at), use_snapshots=not no_snapshots)
    if summary:
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["resource_type"]] = counts.get(r["resource_type"], 0) + 1
        typer.echo(json.dumps({"resources": len(rows), "by_type": dict(sorted(counts.items()))}))
    else:
        typer.echo(json.dumps(rows, default=str))


@app.command("history")
def history_cmd(
    id: str = typer.Option(...),
    limit: int = typer.Option(100),
) -> None:
    """A resource's event history, oldest first."""
    typer.echo(json.dumps(queries.history(store.get_catalog(), id, limit)))


@app.command("diff")
def diff_cmd(
    from_t: str = typer.Option(..., "--from"),
    to_t: str = typer.Option(..., "--to"),
) -> None:
    """Created / deleted / changed resources between two event times."""
    typer.echo(json.dumps(queries.diff(store.get_catalog(), _t(from_t), _t(to_t))))


@app.command("maintain")
def maintain_cmd() -> None:
    """Expire old Iceberg snapshots (prunes the metadata log; disk
    reclamation is engine territory — see the JSON it prints)."""
    typer.echo(json.dumps(store.maintain(store.get_catalog())))
