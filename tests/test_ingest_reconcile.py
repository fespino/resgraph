"""The second count: what the trail recorded against what the sink
holds, and how long since anything was recorded at all."""

import sqlite3
from datetime import datetime

import pytest
from typer.testing import CliRunner

from resgraph.ingest import cli, reconcile, worker
from resgraph.ingest.sink import Sink


def _audit(path, runs):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events (run_id TEXT, seq INTEGER, kind TEXT, payload TEXT,"
        " latency_ms INTEGER, tokens INTEGER, ts TEXT, row_hash TEXT)"
    )
    for run_id, count in runs.items():
        for seq in range(count):
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                (run_id, seq, "tool_call", "{}", 5, None, f"2026-08-20T10:00:{seq:02d}+00:00", "h"),
            )
    conn.commit()
    conn.close()
    return path


def _sink(path, runs):
    sink = Sink(path)
    for run_id, count in runs.items():
        sink.write([worker.enrich(e) for e in worker.synth_batch(run_id, count)])
    sink.close()
    return path


def test_the_counts_agree_when_everything_arrived(tmp_path):
    audit = _audit(tmp_path / "a.db", {"run-0": 4, "run-1": 3})
    sink = _sink(tmp_path / "s.duckdb", {"run-0": 4, "run-1": 3})
    assert reconcile.reconcile(audit, sink) == []


def test_every_kind_of_gap_is_named(tmp_path):
    audit = _audit(tmp_path / "a.db", {"lost": 5, "short": 4})
    sink = _sink(tmp_path / "s.duckdb", {"short": 2, "orphan": 3})
    assert reconcile.reconcile(audit, sink) == [
        "lost: 5 events recorded, nothing in the sink",
        "orphan: 3 rows in the sink the trail never recorded",
        "short: 4 events recorded, 2 rows in the sink",
    ]


def test_silence_is_its_own_question(tmp_path):
    """Both counts agree at zero when a producer stops, so absence
    cannot be a reconciliation finding — it needs its own answer."""
    audit = _audit(tmp_path / "a.db", {"run-0": 2})
    newest = datetime.fromisoformat("2026-08-20T10:00:01+00:00").timestamp()
    assert reconcile.silence_seconds(audit, now=newest) < 60
    assert reconcile.silence_seconds(audit, now=newest + 7200) > 7000
    empty = tmp_path / "e.db"
    sqlite3.connect(empty).execute("CREATE TABLE events (run_id TEXT, ts TEXT)")
    assert reconcile.silence_seconds(empty) is None
    naive = tmp_path / "n.db"
    conn = sqlite3.connect(naive)
    conn.execute("CREATE TABLE events (run_id TEXT, ts TEXT)")
    conn.execute("INSERT INTO events VALUES ('r', '2026-08-20T10:00:00')")  # no offset recorded
    conn.commit()
    conn.close()
    assert reconcile.silence_seconds(naive, now=newest) == pytest.approx(1.0, abs=1)


def test_an_empty_trail_is_reported_as_such(tmp_path):
    empty = tmp_path / "e.db"
    conn = sqlite3.connect(empty)
    conn.execute("CREATE TABLE events (run_id TEXT, ts TEXT)")
    conn.commit()
    conn.close()
    sink = _sink(tmp_path / "s.duckdb", {})
    result = CliRunner().invoke(
        cli.app, ["reconcile", "--audit-db", str(empty), "--sink-path", str(sink)]
    )
    assert result.exit_code == 1
    assert "the trail holds no events at all" in result.output


def test_the_cli_refuses_on_a_gap_and_reports_when_clean(tmp_path):
    audit = _audit(tmp_path / "a.db", {"run-0": 4})
    sink = _sink(tmp_path / "s.duckdb", {"run-0": 2})
    args = ["reconcile", "--audit-db", str(audit), "--sink-path", str(sink)]
    bad = CliRunner().invoke(cli.app, args)
    assert bad.exit_code == 1
    assert "MISMATCH run-0: 4 events recorded, 2 rows in the sink" in bad.output

    matched = _sink(tmp_path / "s2.duckdb", {"run-0": 4})
    good = CliRunner().invoke(
        cli.app,
        [
            "reconcile",
            "--audit-db",
            str(audit),
            "--sink-path",
            str(matched),
            "--quiet-after-s",
            "1e12",
        ],
    )
    assert good.exit_code == 0, good.output
    assert "reconciled; newest event" in good.output


def test_a_stopped_producer_is_reported_even_when_the_counts_agree(tmp_path):
    audit = _audit(tmp_path / "a.db", {"run-0": 4})
    sink = _sink(tmp_path / "s.duckdb", {"run-0": 4})
    result = CliRunner().invoke(
        cli.app,
        ["reconcile", "--audit-db", str(audit), "--sink-path", str(sink), "--quiet-after-s", "1"],
    )
    assert result.exit_code == 1
    assert "nothing recorded for" in result.output
