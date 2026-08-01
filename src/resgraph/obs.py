"""Telemetry (D17): wide events are the log, OTel metrics are the view.

Events: one JSON line per unit of work, appended under
data/telemetry/ — the system of record, DuckDB-queryable, never
shipped through the stream it watches. Metrics: OpenTelemetry
instruments exported to Prometheus — the derived, disposable view.
Without init_metrics() the instruments are no-ops (OTel's default
provider), so library code records unconditionally and only processes
that opt in pay for it.
"""

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import CallbackOptions, Observation

DEFAULT_TELEMETRY_DIR = "data/telemetry"

# The 0.6 boundary IS the D18 composite threshold: good events are
# counted by the bucket, never interpolated from a quantile.
API_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.6, 1.0, 2.5, 5.0, 10.0)
BATCH_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class EventSink:
    """Append-only NDJSON writer, one file per component."""

    def __init__(self, component: str, directory: str | os.PathLike | None = None) -> None:
        d = Path(directory or os.environ.get("RESGRAPH_TELEMETRY_DIR", DEFAULT_TELEMETRY_DIR))
        d.mkdir(parents=True, exist_ok=True)
        self.component = component
        self.path = d / f"{component}.ndjson"
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields) -> None:
        event = {"ts": datetime.now(UTC).isoformat(), "component": self.component, "kind": kind}
        event.update(fields)
        line = json.dumps(event, default=str)
        with self._lock, self.path.open("a") as f:
            f.write(line + "\n")


_meter = otel_metrics.get_meter("resgraph")

READ = _meter.create_counter("ingest_read", description="entries read from the stream")
APPLIED = _meter.create_counter("ingest_applied", description="messages applied")
SKIPPED = _meter.create_counter("ingest_skipped", description="stale messages skipped (D3)")
INVALID = _meter.create_counter("ingest_invalid", description="parse-poison entries dropped")
DLQ = _meter.create_counter("ingest_dlq", description="entries dead-lettered (D14)")
BATCH_SECONDS = _meter.create_histogram(
    "ingest_batch_apply_seconds", description="one apply transaction"
)
API_SECONDS = _meter.create_histogram(
    "api_request_seconds", description="request latency by route and answering store"
)

# ingest_lag readers, registered per consumer; the gauge callback runs
# at scrape time so the reading is as fresh as the scrape.
_lag_readers: dict[str, Callable[[], int | None]] = {}
_lag_lock = threading.Lock()


def register_lag_reader(worker: str, fn: Callable[[], int | None]) -> None:
    with _lag_lock:
        _lag_readers[worker] = fn


def unregister_lag_reader(worker: str) -> None:
    with _lag_lock:
        _lag_readers.pop(worker, None)


def _observe_lag(options: CallbackOptions):
    with _lag_lock:
        readers = dict(_lag_readers)
    for worker, fn in readers.items():
        try:
            lag = fn()
        except Exception:
            lag = None  # a broken reader must not kill the scrape
        if lag is not None:
            yield Observation(lag, {"worker": worker})


_meter.create_observable_gauge(
    "ingest_lag",
    callbacks=[_observe_lag],
    description="messages behind the stream head (broker's view)",
)


def init_metrics(port: int | None = None) -> None:
    """Install the SDK provider + Prometheus reader (idempotent).

    port=None registers the collector without serving it — the API
    mounts /metrics itself; workers pass a port and get an HTTP
    exporter endpoint. Idempotence is read off the provider itself:
    once the SDK MeterProvider is installed, there is nothing to do.
    """
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    if isinstance(otel_metrics.get_meter_provider(), MeterProvider):
        return

    views = [
        View(
            instrument_name="api_request_seconds",
            aggregation=ExplicitBucketHistogramAggregation(API_BUCKETS),
        ),
        View(
            instrument_name="ingest_batch_apply_seconds",
            aggregation=ExplicitBucketHistogramAggregation(BATCH_BUCKETS),
        ),
    ]
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()], views=views)
    )
    _refresh_instruments()
    if port:
        from prometheus_client import start_http_server

        start_http_server(port)


def _refresh_instruments() -> None:
    """Re-create instruments on the real provider.

    Instruments created before set_meter_provider bind to the no-op
    meter; module globals are swapped so callers holding
    ``obs.APPLIED`` style references keep working.
    """
    global _meter, READ, APPLIED, SKIPPED, INVALID, DLQ, BATCH_SECONDS, API_SECONDS
    _meter = otel_metrics.get_meter("resgraph")
    READ = _meter.create_counter("ingest_read", description="entries read from the stream")
    APPLIED = _meter.create_counter("ingest_applied", description="messages applied")
    SKIPPED = _meter.create_counter("ingest_skipped", description="stale messages skipped (D3)")
    INVALID = _meter.create_counter("ingest_invalid", description="parse-poison entries dropped")
    DLQ = _meter.create_counter("ingest_dlq", description="entries dead-lettered (D14)")
    BATCH_SECONDS = _meter.create_histogram(
        "ingest_batch_apply_seconds", description="one apply transaction"
    )
    API_SECONDS = _meter.create_histogram(
        "api_request_seconds", description="request latency by route and answering store"
    )
    _meter.create_observable_gauge(
        "ingest_lag",
        callbacks=[_observe_lag],
        description="messages behind the stream head (broker's view)",
    )


_sinks: dict[tuple[str, str], EventSink] = {}


def get_sink(component: str) -> EventSink:
    """Cached per (component, directory) so a changed env var — tests —
    gets a fresh sink instead of the first directory ever seen."""
    d = os.environ.get("RESGRAPH_TELEMETRY_DIR", DEFAULT_TELEMETRY_DIR)
    key = (component, d)
    if key not in _sinks:
        _sinks[key] = EventSink(component, d)
    return _sinks[key]
