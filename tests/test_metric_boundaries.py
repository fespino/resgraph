"""Every metric the harness computes crosses each boundary as a named
input or stays behind as a recorded exclusion (D52).

This exists because the router was blind to latency for months — not
because the axis went unmeasured, but because the table builder never
copied the column, and nothing downstream had ever seen it, so no test
had an expectation to violate. A dropped metric is invisible in exactly
the way a dropped control is: everything still passes.

So the producers are read, not listed. Calling them on a committed run
discovers what they actually return today, and a metric that is neither
carried forward nor excluded with a reason fails here.
"""

import json
from pathlib import Path

import pytest

from resgraph.evals.arms import (
    NOT_ROUTING_INPUTS,
    NOT_SUMMARISED,
    ROUTING_INPUTS,
    arm_summary,
)
from resgraph.evals.report import aggregate
from resgraph.gateway.quality import AXES, load_quality
from resgraph.ingest.sink import COLUMNS
from resgraph.ingest.worker import NOT_PROJECTED, enrich, synth_batch

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "evals/runs/20260803T121152Z.jsonl"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return [json.loads(line) for line in RUN.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def summary(rows) -> dict:
    return arm_summary("haiku", rows)


def test_no_aggregate_metric_reaches_the_arm_summary_by_being_forgotten(rows):
    produced = set(aggregate(rows))
    forwarded = set(arm_summary("haiku", rows))
    unclassified = produced - forwarded - set(NOT_SUMMARISED)
    assert not unclassified, (
        f"aggregate() produces {sorted(unclassified)}, which arm_summary neither carries "
        "nor lists in NOT_SUMMARISED — decide whether an arm is ranked on it"
    )


def test_no_arm_metric_reaches_the_router_by_being_forgotten(summary):
    unclassified = set(summary) - set(ROUTING_INPUTS) - set(NOT_ROUTING_INPUTS)
    assert not unclassified, (
        f"arm_summary produces {sorted(unclassified)}, which is neither a ROUTING_INPUT "
        "nor an exclusion with a reason"
    )


def test_every_exclusion_names_something_that_exists_and_says_why(rows, summary):
    stale_summary = set(NOT_SUMMARISED) - set(aggregate(rows))
    stale_routing = set(NOT_ROUTING_INPUTS) - set(summary)
    assert not stale_summary and not stale_routing
    reasons = list(NOT_SUMMARISED.values()) + list(NOT_ROUTING_INPUTS.values())
    assert all(len(reason) > 20 for reason in reasons)


def test_the_router_reads_every_field_the_builder_writes(summary, tmp_path):
    """The far side of the same boundary: a field the builder emits and
    the loader silently drops is the identical failure, mirrored."""
    written = {field for field, _ in ROUTING_INPUTS.values()}
    entry = {field: 0 for field in written} | {"run": "r", "date": "2026-08-21"}
    loaded = load_quality(
        json.dumps({"scores": {"judgment": {"a": entry}}})  # JSON is a subset of YAML
    )["judgment"]["a"]
    assert written <= set(loaded), f"the loader drops {sorted(written - set(loaded))}"


def test_every_dominance_axis_is_a_field_the_builder_actually_emits():
    written = {field for field, _ in ROUTING_INPUTS.values()}
    axes = {name for name, _ in AXES}
    assert axes <= written, f"the router ranks on {sorted(axes - written)}, which nothing writes"


def test_the_enrichment_worker_projects_every_field_its_producer_emits():
    """The same boundary at the ingest layer. The spool accepts any
    dict, so this covers the shape our own producer writes, not an
    arbitrary feed's."""
    event = synth_batch("run-boundary", 1)[0]
    row = enrich(event)
    unprojected = set(event) - set(row) - set(NOT_PROJECTED)
    assert not unprojected, f"enrich drops {sorted(unprojected)} with no reason recorded"
    assert set(row) == set(COLUMNS), "the row and the table disagree about the schema"
    nested = set(event["run"]) - {k.removeprefix("run_") for k in row if k.startswith("run_")}
    assert not nested, f"run properties {sorted(nested)} reach no column"
