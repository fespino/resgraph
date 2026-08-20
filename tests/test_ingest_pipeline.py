"""The raw-first pipeline's three graded properties: backpressure
isolation, at-least-once with dedup, and replay-from-raw."""

import json

import pytest
from typer.testing import CliRunner

from resgraph.ingest import cli, worker
from resgraph.ingest.sink import Sink
from resgraph.ingest.spool import RefQueue, Spool


@pytest.fixture
def parts(tmp_path):
    spool = Spool(tmp_path / "raw")
    queue = RefQueue(tmp_path / "queue.db")
    sink = Sink(tmp_path / "sink.duckdb")
    yield spool, queue, sink
    queue.close()
    sink.close()


def test_the_spool_is_content_addressed(parts):
    spool, _, _ = parts
    batch = worker.synth_batch("run-1", 3)
    first = spool.write(batch)
    again = spool.write(batch)
    assert again == first
    assert spool.refs() == [first]
    assert spool.read(first) == batch
    with pytest.raises(FileNotFoundError, match="raw is the source"):
        spool.read("deadbeef")


def test_enrichment_propagates_the_run_and_prices_the_call():
    llm, tool = worker.synth_batch("run-1", 2)
    row = worker.enrich(llm)
    assert row["event_key"] == "run-1:0"
    assert row["run_model"] == "claude-haiku-4-5"
    assert row["cost_usd"] == pytest.approx((900 * 1.0 + 100 * 5.0) / 1_000_000)
    assert json.loads(row["payload"])["turn"] == 0
    assert worker.enrich(tool)["cost_usd"] == 0.0


def test_the_producer_path_does_not_move_while_the_backlog_grows(parts):
    """The whole claim of putting a queue between ingest and the store:
    nothing drains here, and the producer still never waits on it."""
    spool, queue, sink = parts
    result = worker.measure_backpressure(spool, queue, sink, batches=60, size=20)
    assert result["backlog"] == 60
    assert result["backlog_drift"] < 3  # the property: a full backlog costs the producer nothing
    assert result["speedup_p50"] > 2
    # a catastrophe budget, not an SLO: loose enough to survive a loaded
    # runner, tight enough to fail if I/O reappears on the producer path
    assert result["queued"]["p50_us"] < 20_000


def test_delivery_is_at_least_once_and_the_sink_absorbs_it(parts):
    spool, queue, sink = parts
    empty = sink.write([])
    assert empty == 0
    ref = spool.write(worker.synth_batch("run-1", 5))
    queue.enqueue(ref)
    first = worker.drain(spool, queue, sink)
    assert first == {"batches": 1, "rows_written": 5}
    queue.enqueue(ref)
    second = worker.drain(spool, queue, sink)
    assert second["batches"] == 1
    assert second["rows_written"] == 0  # redelivered, already recorded
    assert sink.count() == 5


def test_a_worker_that_dies_mid_batch_leaves_the_claim_reclaimable(parts, monkeypatch):
    spool, queue, sink = parts
    queue.enqueue(spool.write(worker.synth_batch("run-1", 4)))

    def boom(_rows):
        raise RuntimeError("sink is down")

    monkeypatch.setattr(sink, "write", boom)
    with pytest.raises(RuntimeError):
        worker.drain(spool, queue, sink)
    assert (queue.pending(), queue.inflight()) == (0, 1)  # unacked: nothing was lost
    reclaimed = queue.reclaim(older_than_s=0.0)
    assert reclaimed
    monkeypatch.undo()
    retried = worker.drain(spool, queue, sink)
    assert retried["rows_written"] == 4


def test_the_queue_only_reclaims_stale_claims(parts):
    _, queue, _ = parts
    queue.enqueue("a", now=100.0)
    claimed = queue.claim(now=100.0)
    assert claimed == ["a"]
    fresh = queue.reclaim(older_than_s=30.0, now=120.0)
    assert fresh == []
    stale = queue.reclaim(older_than_s=30.0, now=140.0)
    assert stale == ["a"]
    assert queue.pending() == 1


def test_the_sink_can_be_dropped_and_rebuilt_from_raw_alone(parts):
    spool, queue, sink = parts
    for i in range(4):
        queue.enqueue(spool.write(worker.synth_batch(f"run-{i}", 6)))
    worker.drain(spool, queue, sink)
    before = sink.digest()
    assert sink.count() == 24

    sink.reset()
    assert sink.count() == 0
    result = worker.replay_from_raw(spool, sink)
    assert result == {"batches": 4, "rows_written": 24}
    assert sink.digest() == before  # the columnar store is disposable


def test_the_cli_walks_the_pipeline(tmp_path):
    paths = [
        "--spool-root",
        str(tmp_path / "raw"),
        "--queue-path",
        str(tmp_path / "queue.db"),
        "--sink-path",
        str(tmp_path / "sink.duckdb"),
    ]
    runner = CliRunner()
    spiked = runner.invoke(cli.app, ["spike", "--batches", "5", "--size", "4", *paths])
    assert spiked.exit_code == 0, spiked.output
    assert json.loads(spiked.output)["backlog"] == 5

    drained = runner.invoke(cli.app, ["drain", *paths])
    assert drained.exit_code == 0, drained.output
    assert "5 batches, 20 rows, 0 pending" in drained.output

    seen = runner.invoke(cli.app, ["stats", *paths])
    assert "raw batches 5" in seen.output and "sink rows 40" in seen.output

    replayed = runner.invoke(
        cli.app,
        [
            "replay",
            "--spool-root",
            str(tmp_path / "raw"),
            "--sink-path",
            str(tmp_path / "sink.duckdb"),
        ],
    )
    assert replayed.exit_code == 0, replayed.output
    # the spike's direct-to-sink arm left no raw behind, so replay cannot
    # restore it: skipping the spool costs replayability, not just latency
    assert "5 batches replayed, 20 rows" in replayed.output
    assert Sink(tmp_path / "sink.duckdb").count() == 20
