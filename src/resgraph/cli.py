"""resgraph — platform CLI (graph store operations + queries)."""

import json
import sys

import typer

from resgraph.graph import queries
from resgraph.graph.client import get_driver
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema
from resgraph.schema import UpdateMessage

app = typer.Typer(help="resgraph platform CLI.", add_completion=False)
query_app = typer.Typer(help="Traversal queries against the hot store.")
app.add_typer(query_app, name="query")


def _session():
    return get_driver().session()


@app.command("schema-init")
def schema_init() -> None:
    """Bootstrap indexes + constraints (idempotent, D8)."""
    with _session() as s:
        init_schema(s)
    typer.echo("schema ok")


@app.command("load-snapshot")
def load_snapshot_cmd() -> None:
    """Load a world snapshot from JSONL on stdin (resgraph-gen seed | ...).

    Refuses a non-empty store — this is the loader, not the ingest."""
    msgs = (UpdateMessage.model_validate_json(line) for line in sys.stdin if line.strip())
    with _session() as s:
        init_schema(s)
        counts = load_snapshot(s, msgs)
    typer.echo(json.dumps(counts))


@query_app.command("blast-radius")
def blast_radius_cmd(
    id: str = typer.Option(...),
    depth: int = typer.Option(3),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
) -> None:
    with _session() as s:
        rows = queries.blast_radius(s, id, depth, include_deleted)
    typer.echo(json.dumps([r.model_dump() for r in rows]))


@query_app.command("path")
def path_cmd(from_id: str = typer.Option(..., "--from"), to_id: str = typer.Option(..., "--to")):
    with _session() as s:
        p = queries.dependency_path(s, from_id, to_id)
    typer.echo(json.dumps(p.model_dump() if p else None))


@query_app.command("orphans")
def orphans_cmd() -> None:
    with _session() as s:
        typer.echo(json.dumps(queries.orphans(s)))


@query_app.command("stats")
def stats_cmd() -> None:
    with _session() as s:
        typer.echo(json.dumps(queries.stats(s)))
