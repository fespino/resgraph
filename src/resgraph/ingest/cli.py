"""resgraph-ingest — the raw-first pipeline: spike, drain, replay.

Decisions: D48 (SPEC.md).
"""

import json
from pathlib import Path

import typer

from resgraph.ingest import layouts, reconcile, worker
from resgraph.ingest.sink import SINK_PATH, Sink
from resgraph.ingest.spool import QUEUE_PATH, SPOOL_ROOT, RefQueue, Spool

app = typer.Typer(help="Raw-first ingest: spool, queue, enrichment worker, columnar sink.")


def _parts(root: str, queue: str, sink: str) -> tuple[Spool, RefQueue, Sink]:
    return Spool(Path(root)), RefQueue(Path(queue)), Sink(Path(sink))


@app.command()
def spike(
    batches: int = 200,
    size: int = 20,
    spool_root: str = str(SPOOL_ROOT),
    queue_path: str = str(QUEUE_PATH),
    sink_path: str = str(SINK_PATH),
) -> None:
    """Producer latency through the queue against writing straight to
    the columnar store, with nothing draining the backlog."""
    spool, queue, sink = _parts(spool_root, queue_path, sink_path)
    typer.echo(
        json.dumps(
            worker.measure_backpressure(spool, queue, sink, batches=batches, size=size), indent=1
        )
    )
    queue.close()
    sink.close()


@app.command()
def drain(
    limit: int = 16,
    spool_root: str = str(SPOOL_ROOT),
    queue_path: str = str(QUEUE_PATH),
    sink_path: str = str(SINK_PATH),
) -> None:
    """One worker pass: claim, enrich, flush, ack."""
    spool, queue, sink = _parts(spool_root, queue_path, sink_path)
    result = worker.drain(spool, queue, sink, limit=limit)
    typer.echo(
        f"{result['batches']} batches, {result['rows_written']} rows, {queue.pending()} pending"
    )
    queue.close()
    sink.close()


@app.command()
def replay(
    spool_root: str = str(SPOOL_ROOT),
    sink_path: str = str(SINK_PATH),
) -> None:
    """Rebuild the sink from raw alone and print the digest that has to
    match what was there before."""
    spool, sink = Spool(Path(spool_root)), Sink(Path(sink_path))
    result = worker.replay_from_raw(spool, sink)
    typer.echo(
        f"{result['batches']} batches replayed, {result['rows_written']} rows,"
        f" digest {sink.digest()[:16]}"
    )
    sink.close()


@app.command(name="layouts")
def layouts_cmd(
    runs: int = 200,
    events_per_run: int = 30,
    repeats: int = 7,
    root: str = "data/ingest/layouts",
) -> None:
    """Wide against normalized (and against normalized with the
    duplicates at-least-once leaves), same rows, same questions.
    Refuses to time anything until the arms agree on every answer."""
    result = layouts.compare(Path(root), runs=runs, events_per_run=events_per_run, repeats=repeats)
    typer.echo(json.dumps(result, indent=1))


@app.command(name="reconcile")
def reconcile_cmd(
    audit_db: str = "data/audit.db",
    sink_path: str = str(SINK_PATH),
    quiet_after_s: float = 86_400,
) -> None:
    """The trail's own count against the sink's, per run, plus how
    long since anything was recorded — a stopped producer keeps every
    component healthy and both counts agreeing at zero."""
    findings = reconcile.reconcile(Path(audit_db), Path(sink_path))
    quiet = reconcile.silence_seconds(Path(audit_db))
    if quiet is None:
        findings.append("the trail holds no events at all")
    elif quiet > quiet_after_s:
        findings.append(f"nothing recorded for {quiet / 3600:.1f}h")
    for finding in findings:
        typer.echo(f"MISMATCH {finding}")
    if findings:
        raise SystemExit(1)
    typer.echo(f"reconciled; newest event {(quiet or 0) / 60:.1f} min old")


@app.command()
def stats(
    spool_root: str = str(SPOOL_ROOT),
    queue_path: str = str(QUEUE_PATH),
    sink_path: str = str(SINK_PATH),
) -> None:
    spool, queue, sink = _parts(spool_root, queue_path, sink_path)
    typer.echo(
        f"raw batches {len(spool.refs())} | pending {queue.pending()} inflight {queue.inflight()}"
        f" | sink rows {sink.count()} digest {sink.digest()[:16]}"
    )
    queue.close()
    sink.close()
