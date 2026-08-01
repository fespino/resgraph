"""The D17 parity test: metrics are views over the event log — proven.

The composite SLI computed two independent ways must agree:
- Prometheus side: the 0.6s bucket over the request count (exact
  good-event counting, D18), taken as a delta across this test's
  requests so other tests' samples don't pollute it.
- Event side: SQL over the raw api_request events with DuckDB — the
  system of record, queried like everything else this platform calls
  truth.
If they disagree beyond rounding at the bucket boundary, one layer is
lying.
"""

import time
from datetime import UTC, datetime

import duckdb
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest

from resgraph import obs
from resgraph.api import app as api_app
from resgraph.cold import store as cold_store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.query.executor import QueryContext

SEED, RESOURCES, CHURN = 42, 100, 400
T_LIVE = datetime(2026, 1, 3, tzinfo=UTC)


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    churn = Churn(World(SEED, RESOURCES))
    msgs = list(churn.snapshot()) + [churn.next_message() for _ in range(CHURN)]
    cat = cold_store.get_catalog(tmp_path_factory.mktemp("parity-cold"))
    cold_store.ensure_tables(cat)
    cold_store.append_events(cat, msgs)
    return cat, msgs


def _prom_composite() -> tuple[float, float]:
    good = total = 0.0
    for ln in generate_latest(REGISTRY).decode().splitlines():
        if 'source="composite"' not in ln:
            continue
        if ln.startswith("api_request_seconds_bucket") and 'le="0.6"' in ln:
            good += float(ln.rsplit(" ", 1)[1])
        elif ln.startswith("api_request_seconds_count"):
            total += float(ln.rsplit(" ", 1)[1])
    return good, total


def test_composite_sli_agrees_between_metrics_and_events(catalog, tmp_path, monkeypatch):
    monkeypatch.setenv("RESGRAPH_TELEMETRY_DIR", str(tmp_path))
    cat, msgs = catalog
    t_end = msgs[-1].event_time.isoformat()
    obs.init_metrics()

    # every third reconstruction crosses the 0.6s threshold, so both
    # layers must count the same requests as slow
    from resgraph.query import executor as ex

    real_state_at = ex.cold_queries.state_at
    calls = {"n": 0}

    def slow_every_third(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            time.sleep(0.65)
        return real_state_at(*args, **kwargs)

    monkeypatch.setattr(ex.cold_queries, "state_at", slow_every_third)

    api_app.app.dependency_overrides[api_app.get_ctx] = lambda: QueryContext(catalog=cat)
    client = TestClient(api_app.app)
    good0, total0 = _prom_composite()
    roots = sorted({m.resource_id for m in msgs})[:12]
    try:
        for root in roots:
            r = client.get(f"/blast-radius/{root}", params={"at": t_end})
            assert r.status_code == 200
    finally:
        api_app.app.dependency_overrides.clear()

    good1, total1 = _prom_composite()
    prom_good, prom_total = good1 - good0, total1 - total0
    assert prom_total == len(roots)

    events_good, events_total = (
        duckdb.connect()
        .execute(
            f"""
        SELECT count(*) FILTER (WHERE ms <= 600.0), count(*)
        FROM read_json_auto('{tmp_path}/api.ndjson')
        WHERE kind = 'api_request' AND source = 'composite'
        """
        )
        .fetchone()
    )

    assert events_total == prom_total
    # same requests, same clock; at most one event can flip on the
    # ms-rounding of the exact boundary
    assert abs(events_good - prom_good) <= 1
    assert prom_good < prom_total, "the slow third must be visible to both layers"
