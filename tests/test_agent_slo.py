"""D29b agent SLO wiring: the dashboard panels reference recording
rules that actually exist, and the analyst instruments record without
a provider installed (no-op) so instrumentation never crashes a run."""

import json
from pathlib import Path

import yaml

from resgraph import obs

RULES = Path("observability/rules/agent_slo.yml")
DASHBOARD = Path("observability/grafana/dashboards/resgraph-overview.json")


def recorded_rules() -> set[str]:
    doc = yaml.safe_load(RULES.read_text())
    return {r["record"] for g in doc["groups"] for r in g["rules"] if "record" in r}


def test_agent_panels_reference_real_rules():
    recorded = recorded_rules()
    dashboard = json.loads(DASHBOARD.read_text())
    referenced = {
        t["expr"]
        for p in dashboard["panels"]
        for t in p.get("targets", [])
        if t["expr"].startswith("slo:analyst_")
    }
    assert referenced, "the dashboard should carry the agent SLO panels"
    assert referenced <= recorded, f"panels reference undefined rules: {referenced - recorded}"


def test_analyst_instruments_record_without_a_provider():
    # No init_metrics: the instruments are no-ops but must not raise.
    obs.ANALYST_RUN_SECONDS.record(12.3)
    obs.ANALYST_RUN_COST.record(0.14)
    obs.ANALYST_RUNS.add(1, {"degraded": "false", "cutoff": "none"})
