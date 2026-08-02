"""Telemetry layer (D17): events are the log, OTel metrics are the view.

No stores needed: the OTel SDK exports through the in-process
Prometheus registry, and events land in the per-test telemetry dir
(conftest). The D18 bucket-at-threshold claim is pinned here: the 0.6
boundary must exist verbatim in the exported histogram.
"""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest

from resgraph import obs


def _scrape() -> bytes:
    return generate_latest(REGISTRY)


def test_counters_and_histograms_export_with_expected_names():
    obs.init_metrics()
    obs.APPLIED.add(3, {"worker": "w-test"})
    obs.BATCH_SECONDS.record(0.05, {"worker": "w-test"})
    obs.API_SECONDS.record(0.2, {"route": "/world", "source": "cold"})
    obs.PHANTOMS_CREATED.add(2)
    out = _scrape()
    assert b"ingest_applied_total" in out
    assert b'worker="w-test"' in out
    assert b"ingest_batch_apply_seconds_bucket" in out
    assert b"api_request_seconds_bucket" in out
    assert b"graph_phantoms_created_total" in out


def test_api_histogram_has_bucket_at_the_slo_threshold():
    obs.init_metrics()
    obs.API_SECONDS.record(0.3, {"route": "/blast-radius/{resource_id}", "source": "composite"})
    out = _scrape().decode()
    assert 'le="0.6"' in out  # D18: good events counted exactly at the boundary
    slow = [ln for ln in out.splitlines() if 'le="0.6"' in ln and "composite" in ln]
    assert slow, "composite series must carry the threshold bucket"


def test_lag_gauge_reads_registered_workers_at_scrape_time():
    obs.init_metrics()
    obs.register_lag_reader("w-lag", lambda: 42)
    try:
        out = _scrape().decode()
        assert "ingest_lag" in out
        assert any('worker="w-lag"' in ln and ln.endswith("42.0") for ln in out.splitlines())
    finally:
        obs.unregister_lag_reader("w-lag")


def test_event_sink_writes_queryable_ndjson(tmp_path, monkeypatch):
    monkeypatch.setenv("RESGRAPH_TELEMETRY_DIR", str(tmp_path))
    sink = obs.get_sink("test-component")
    sink.emit("unit_test", answer=42, when=datetime.now(UTC))
    lines = (tmp_path / "test-component.ndjson").read_text().splitlines()
    event = json.loads(lines[-1])
    assert event["kind"] == "unit_test" and event["answer"] == 42
    assert event["component"] == "test-component" and "ts" in event


def test_api_emits_wide_event_and_metric_without_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("RESGRAPH_TELEMETRY_DIR", str(tmp_path))
    from resgraph.api import app as api_app

    client = TestClient(api_app.app)
    r = client.get(
        "/blast-radius/vm-000001",
        params={"at": "2026-01-03T00:00:00+00:00", "explain": "true"},
    )
    assert r.status_code == 200
    events = [json.loads(ln) for ln in (tmp_path / "api.ndjson").read_text().splitlines()]
    e = events[-1]
    assert e["kind"] == "api_request"
    assert e["route"] == "/blast-radius/{resource_id}"
    assert e["source"] == "plan" and e["status"] == 200
    assert e["root"] == "vm-000001"
    assert "explain=true" in e["query"]
    out = _scrape().decode()
    assert 'route="/blast-radius/{resource_id}"' in out


def test_metrics_endpoint_is_mounted():
    from resgraph.api import app as api_app

    client = TestClient(api_app.app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "api_request_seconds" in r.text


def test_event_content_policy_no_payloads_no_unbounded_fields(tmp_path, monkeypatch):
    """D17 content policy: identifiers, dimensions, timings, counts —
    never payload bodies. Fields are size-bounded so an event can never
    smuggle a message body into the forever-log."""
    import json as _json

    monkeypatch.setenv("RESGRAPH_TELEMETRY_DIR", str(tmp_path))
    from resgraph.api import app as api_app

    client = TestClient(api_app.app)
    client.get(
        "/blast-radius/vm-000001",
        params={"at": "2026-01-03T00:00:00+00:00", "explain": "true", "filter": "attrs.zone=z1"},
    )
    events = [_json.loads(ln) for ln in (tmp_path / "api.ndjson").read_text().splitlines()]
    assert events
    for e in events:
        assert "attrs" not in e and "payload" not in e and "data" not in e
        for key, value in e.items():
            assert len(str(value)) <= 600, (key, value)


def test_broken_lag_reader_does_not_kill_the_scrape():
    obs.init_metrics()

    def broken():
        raise RuntimeError("reader died")

    obs.register_lag_reader("w-broken", broken)
    try:
        out = _scrape().decode()
        assert 'worker="w-broken"' not in out  # skipped, not fatal (D17)
    finally:
        obs.unregister_lag_reader("w-broken")
