"""Coverage-gap instrument (#180): the generator plants the gap the
way it plants the cause."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from resgraph.evals.runner import withheld_events

ITEM = Path("evals/scenarios/gap-pilot.jsonl")


def _msg(seq, t):
    return SimpleNamespace(sequence=seq, event_time=datetime(2026, 1, 1, 0, 0, t, tzinfo=UTC))


def test_withheld_events_cuts_churn_but_keeps_the_snapshot():
    msgs = [_msg(0, 0), _msg(1, 1), _msg(2, 2), _msg(3, 3)]
    kept = withheld_events(msgs, datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC).isoformat())
    assert [(m.sequence) for m in kept] == [0, 2, 3]


def test_no_gap_means_no_filtering():
    msgs = [_msg(0, 0), _msg(1, 1)]
    assert withheld_events(msgs, None) is msgs


def test_the_pilot_item_withholds_the_cause_and_nothing_after_it():
    from resgraph.gen.scenarios import Scenario, rebuild

    spec = Scenario.model_validate_json(ITEM.read_text())
    assert "coverage_gap" in spec.tags
    truth = spec.ground_truth
    assert truth is not None
    gen = rebuild(spec)
    kept = withheld_events(gen.messages, str(spec.provenance["gap_before"]))
    assert not any(m.sequence == truth.causal_sequence for m in kept)
    assert any(m.sequence > 0 for m in kept), "the log must resume after the cut"
    assert sum(1 for m in kept if m.sequence == 0) == sum(
        1 for m in gen.messages if m.sequence == 0
    )


def test_the_pilot_item_world_is_unchanged_from_its_parent():
    base = {
        json.loads(line)["id"]: json.loads(line)
        for line in Path("evals/scenarios/base.jsonl").read_text().splitlines()
        if line.strip()
    }
    item = json.loads(ITEM.read_text())
    parent = base[item["id"].removesuffix("-gap")]
    assert item["seed"] == parent["seed"]
    assert item["ground_truth"] == parent["ground_truth"]


def test_a_deferred_report_serializes_into_a_run_row():
    """A deferral carries datetimes; python-mode model_dump would crash
    json.dumps AFTER the paid call. Caught by the pre-run verification
    pass, pinned here."""
    from datetime import timedelta

    from resgraph.analyst.models import TriageReport

    report = TriageReport(
        suspects=[],
        no_confident_candidate=True,
        deferral={
            "store": "cold",
            "window_start": datetime(2026, 1, 1, tzinfo=UTC),
            "window_end": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=1),
            "would_decide": "x",
        },
        narrative="n",
    )
    json.dumps({"report": report.model_dump(mode="json")})
    runner_src = Path("src/resgraph/evals/runner.py").read_text()
    assert '"report": result.report.model_dump(mode="json")' in runner_src
