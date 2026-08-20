"""Wide against normalized: the arms must hold the same data before
any timing between them means anything."""

import duckdb
import pytest
from typer.testing import CliRunner

from resgraph.ingest import cli, layouts


def test_the_dedup_arm_carries_the_duplicates_at_least_once_leaves(tmp_path):
    rows = layouts.wide_rows(4, 10)
    plain, dup = tmp_path / "n.duckdb", tmp_path / "d.duckdb"
    layouts.build_normalized(plain, rows)
    layouts.build_normalized(dup, rows, duplicate_every=5)
    counts = []
    for path in (plain, dup):
        con = duckdb.connect(str(path), read_only=True)
        counts.append(con.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        con.close()
    assert counts == [40, 48]


def test_the_dedup_arm_reads_through_the_duplicates(tmp_path):
    rows = layouts.wide_rows(4, 10)
    dup = tmp_path / "d.duckdb"
    layouts.build_normalized(dup, rows, duplicate_every=5)
    plain_answers = layouts.answers(dup, "normalized")
    deduped = layouts.answers(dup, "normalized_dedup")
    assert plain_answers["cost_by_model"] != deduped["cost_by_model"]  # duplicates double-count
    wide = tmp_path / "w.duckdb"
    layouts.build_wide(wide, rows)
    assert deduped == layouts.answers(wide, "wide")


def test_disagreeing_arms_refuse_to_be_timed(tmp_path, monkeypatch):
    """The invariant that makes the comparison mean anything: if the
    layouts hold different data, no timing is reported at all."""
    original = layouts.build_normalized

    def short(path, rows, **kwargs):
        original(path, rows[:-5], **kwargs)

    monkeypatch.setattr(layouts, "build_normalized", short)
    with pytest.raises(SystemExit, match="would measure nothing"):
        layouts.compare(tmp_path / "cmp", runs=3, events_per_run=10, repeats=1)


def test_the_comparison_reports_every_arm(tmp_path):
    result = layouts.compare(tmp_path / "cmp", runs=6, events_per_run=10, repeats=2)
    assert result["rows"] == 60
    assert set(result["layouts"]) == {"wide", "normalized", "normalized_dedup"}
    for arm in result["layouts"].values():
        assert arm["bytes"] > 0
        assert set(arm["queries"]) == set(layouts.QUERIES)


def test_the_cli_prints_the_comparison(tmp_path):
    result = CliRunner().invoke(
        cli.app,
        [
            "layouts",
            "--runs",
            "4",
            "--events-per-run",
            "10",
            "--repeats",
            "1",
            "--root",
            str(tmp_path / "cmp"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"normalized_dedup"' in result.output
