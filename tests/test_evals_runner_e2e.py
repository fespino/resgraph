"""run_eval end to end against real stores with a fake API client:
the loop that spends money in paid runs, exercised for free (#130).
The fake client always returns a complete, valid hedge report, so
every store-loading, grading, pinning, resume, and cost-brake path
runs without a token spent."""

import json
import os
from types import SimpleNamespace

import pytest

from resgraph.evals.runner import run_eval
from resgraph.gen.scenarios import generate_set
from resgraph.graph.client import get_driver

pytestmark = pytest.mark.integration

REPORT = json.dumps(
    {
        "suspects": [],
        "no_confident_candidate": True,
        "narrative": "quiet window; no confident candidate",
    }
)


class HedgingClient:
    def __init__(self):
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=REPORT)],
            usage=SimpleNamespace(
                input_tokens=1_000,
                output_tokens=100,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


@pytest.fixture(scope="module")
def driver():
    try:
        d = get_driver()
        d.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d)")
    yield d
    d.close()


@pytest.fixture()
def scenarios():
    return [g.spec for g in generate_set(seed=9, count=3)]


def test_run_writes_pinned_rows_and_resume_skips(driver, scenarios, tmp_path):
    out = run_eval(
        scenarios,
        HedgingClient(),
        driver,
        model="claude-opus-4-8",
        judge_model=None,
        trials=1,
        out_dir=tmp_path,
    )
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 3
    for row in rows:
        assert row["model"] == "claude-opus-4-8"
        assert "@sha256:" in row["stores"]["memgraph"]
        assert row["host"]["class"] in {"ci", "laptop"}
        assert row["source"] == "planted"
        assert row["git_ref"]
        assert row["cache_fingerprint"]
        assert row["tokens"]["total"] > 0
    control = next(r for r in rows if r["scenario_type"] == "control")
    assert control["dims"]["honesty"]["passed"] is True
    causal = next(r for r in rows if r["scenario_type"] != "control")
    assert causal["dims"]["found_top3"]["passed"] is False

    again = run_eval(
        scenarios,
        HedgingClient(),
        driver,
        model="claude-opus-4-8",
        judge_model=None,
        trials=1,
        out_dir=tmp_path,
        resume_path=out,
    )
    assert again == out
    assert len(out.read_text().splitlines()) == 3


def test_max_cost_stops_resume_ready(driver, scenarios, tmp_path, capsys):
    out = run_eval(
        scenarios,
        HedgingClient(),
        driver,
        model="claude-opus-4-8",
        judge_model=None,
        trials=1,
        out_dir=tmp_path,
        max_cost=1e-6,
    )
    assert len(out.read_text().splitlines()) == 1
    assert "--resume" in capsys.readouterr().out
