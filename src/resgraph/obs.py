# pyright: reportConstantRedefinition=false
# (the UPPER_CASE instruments are rebindable by design: _refresh_instruments
# swaps them onto the real provider after init_metrics installs it)
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
# Analyst SLO boundaries (D29b), objectives from the certified k=3
# baseline (evals/baseline.json): p95 latency 101.3 s -> 152 s good
# (1.5x, rounded to a bucket edge); cost/run ~$0.14 -> $0.30 good
# (2x headroom). The boundary IS a bucket edge so the good-event ratio
# is counted, never interpolated (D18).
ANALYST_RUN_BUCKETS = (10.0, 30.0, 60.0, 120.0, 152.0, 240.0, 480.0)
ANALYST_COST_BUCKETS = (0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0)
GATEWAY_TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
GATEWAY_TPS_BUCKETS = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)
GATEWAY_CHAIN_BUCKETS = (0.0, 1.0, 2.0)
GATEWAY_COST_BUCKETS = (0.0005, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.3)


class EventSink:
    """Append-only NDJSON writer, one file per component."""

    def __init__(self, component: str, directory: str | os.PathLike[str] | None = None) -> None:
        d = Path(directory or os.environ.get("RESGRAPH_TELEMETRY_DIR", DEFAULT_TELEMETRY_DIR))
        d.mkdir(parents=True, exist_ok=True)
        self.component = component
        self.path = d / f"{component}.ndjson"
        self._lock = threading.Lock()

    def emit(self, kind: str, **fields: object) -> None:
        event: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "component": self.component,
            "kind": kind,
        }
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
PHANTOMS_CREATED = _meter.create_counter(
    "graph_phantoms_created", description="phantom placeholder nodes created (D17 addendum)"
)
BATCH_SECONDS = _meter.create_histogram(
    "ingest_batch_apply_seconds", description="one apply transaction"
)
API_SECONDS = _meter.create_histogram(
    "api_request_seconds", description="request latency by route and answering store"
)

# Analyst runtime metrics (D29b). Instrument-before-subject: the SLO
# rules are wired against these names now; the series populate when the
# agent runs as a scraped service (#145).
ANALYST_RUN_SECONDS = _meter.create_histogram(
    "analyst_run_seconds", description="one triage run, wall clock"
)
ANALYST_RUN_COST = _meter.create_histogram(
    "analyst_run_cost_usd", description="estimated worker cost of one triage run"
)
ANALYST_RUNS = _meter.create_counter(
    "analyst_runs_total", description="triage runs, labeled by degraded + cutoff reason"
)

# Gateway token-path metrics (the serving decisions' names). Instrument-
# before-subject, like the analyst set: SLO rules wire against these now.
GATEWAY_TTFT = _meter.create_histogram(
    "gateway_ttft_seconds", description="first-content-token wait, by backend and cache state"
)
GATEWAY_TOKENS_PER_S = _meter.create_histogram(
    "gateway_tokens_per_second", description="generation rate over the emission window"
)
GATEWAY_REQUESTS = _meter.create_counter(
    "gateway_requests_total", description="requests by backend, outcome, source, task class"
)
GATEWAY_FALLBACK_CHAIN = _meter.create_histogram(
    "gateway_fallback_chain_length", description="hops walked before a request was served"
)
GATEWAY_STREAM_ERRORS = _meter.create_counter(
    "gateway_stream_errors_total", description="mid-stream deaths, zero vs nonzero tokens emitted"
)
GATEWAY_CACHE_HITS = _meter.create_counter(
    "gateway_cache_hits_total",
    description="cache hits by layer (gateway response / provider prefix)",
)
GATEWAY_CACHE_MISSES = _meter.create_counter(
    "gateway_cache_misses_total",
    description="cache misses by layer — hits alone flatter the meter",
)
GATEWAY_CACHE_TOKENS_SAVED = _meter.create_counter(
    "gateway_cache_tokens_saved", description="tokens the gateway response cache did not spend"
)
GATEWAY_COST = _meter.create_histogram(
    "gateway_cost_usd", description="estimated cost per request, by task class, backend, source"
)
GATEWAY_FALLBACK_SPEND = _meter.create_counter(
    "gateway_fallback_spend_usd", description="money the failure path spent falling forward"
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


_gateway_depth_reader: Callable[[], list[tuple[str, int]]] | None = None
_gateway_depth_lock = threading.Lock()


def register_gateway_depth_reader(fn: Callable[[], list[tuple[str, int]]]) -> None:
    """One reader per process: the gateway registers a callback returning
    (backend, in-flight depth) pairs; the gauge reads it at scrape time."""
    global _gateway_depth_reader
    with _gateway_depth_lock:
        _gateway_depth_reader = fn


def _observe_gateway_depth(options: CallbackOptions):
    with _gateway_depth_lock:
        fn = _gateway_depth_reader
    if fn is None:
        return
    try:
        depths = fn()
    except Exception:
        return  # a broken reader must not kill the scrape
    for backend, depth in depths:
        yield Observation(depth, {"backend": backend})


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
    "gateway_queue_depth",
    callbacks=[_observe_gateway_depth],
    description="in-flight requests per backend — the leading pressure signal",
)
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
        View(
            instrument_name="analyst_run_seconds",
            aggregation=ExplicitBucketHistogramAggregation(ANALYST_RUN_BUCKETS),
        ),
        View(
            instrument_name="analyst_run_cost_usd",
            aggregation=ExplicitBucketHistogramAggregation(ANALYST_COST_BUCKETS),
        ),
        View(
            instrument_name="gateway_ttft_seconds",
            aggregation=ExplicitBucketHistogramAggregation(GATEWAY_TTFT_BUCKETS),
        ),
        View(
            instrument_name="gateway_tokens_per_second",
            aggregation=ExplicitBucketHistogramAggregation(GATEWAY_TPS_BUCKETS),
        ),
        View(
            instrument_name="gateway_fallback_chain_length",
            aggregation=ExplicitBucketHistogramAggregation(GATEWAY_CHAIN_BUCKETS),
        ),
        View(
            instrument_name="gateway_cost_usd",
            aggregation=ExplicitBucketHistogramAggregation(GATEWAY_COST_BUCKETS),
        ),
    ]
    otel_metrics.set_meter_provider(
        MeterProvider(metric_readers=[PrometheusMetricReader()], views=views)
    )
    _refresh_instruments()
    if port:
        _serve_metrics(port)


def _serve_metrics(port: int):
    # deferred like init_metrics' SDK imports: every component imports
    # obs, only metrics-serving processes pay for the exporter
    from prometheus_client import start_http_server

    # loopback; the obs-profile Prometheus reaches it via host.docker.internal
    return start_http_server(port, addr="127.0.0.1")


def _refresh_instruments() -> None:
    """Re-create instruments on the real provider.

    Instruments created before set_meter_provider bind to the no-op
    meter; module globals are swapped so callers holding
    ``obs.APPLIED`` style references keep working.
    """
    global _meter, READ, APPLIED, SKIPPED, INVALID, DLQ, PHANTOMS_CREATED
    global BATCH_SECONDS, API_SECONDS
    global ANALYST_RUN_SECONDS, ANALYST_RUN_COST, ANALYST_RUNS
    global GATEWAY_TTFT, GATEWAY_TOKENS_PER_S, GATEWAY_REQUESTS, GATEWAY_FALLBACK_CHAIN
    global GATEWAY_STREAM_ERRORS, GATEWAY_CACHE_HITS, GATEWAY_CACHE_MISSES
    global GATEWAY_CACHE_TOKENS_SAVED, GATEWAY_COST, GATEWAY_FALLBACK_SPEND
    _meter = otel_metrics.get_meter("resgraph")
    READ = _meter.create_counter("ingest_read", description="entries read from the stream")
    APPLIED = _meter.create_counter("ingest_applied", description="messages applied")
    SKIPPED = _meter.create_counter("ingest_skipped", description="stale messages skipped (D3)")
    INVALID = _meter.create_counter("ingest_invalid", description="parse-poison entries dropped")
    DLQ = _meter.create_counter("ingest_dlq", description="entries dead-lettered (D14)")
    PHANTOMS_CREATED = _meter.create_counter(
        "graph_phantoms_created", description="phantom placeholder nodes created (D17 addendum)"
    )
    BATCH_SECONDS = _meter.create_histogram(
        "ingest_batch_apply_seconds", description="one apply transaction"
    )
    API_SECONDS = _meter.create_histogram(
        "api_request_seconds", description="request latency by route and answering store"
    )
    ANALYST_RUN_SECONDS = _meter.create_histogram(
        "analyst_run_seconds", description="one triage run, wall clock"
    )
    ANALYST_RUN_COST = _meter.create_histogram(
        "analyst_run_cost_usd", description="estimated worker cost of one triage run"
    )
    ANALYST_RUNS = _meter.create_counter(
        "analyst_runs_total", description="triage runs, labeled by degraded + cutoff reason"
    )
    GATEWAY_TTFT = _meter.create_histogram(
        "gateway_ttft_seconds", description="first-content-token wait, by backend and cache state"
    )
    GATEWAY_TOKENS_PER_S = _meter.create_histogram(
        "gateway_tokens_per_second", description="generation rate over the emission window"
    )
    GATEWAY_REQUESTS = _meter.create_counter(
        "gateway_requests_total", description="requests by backend, outcome, source, task class"
    )
    GATEWAY_FALLBACK_CHAIN = _meter.create_histogram(
        "gateway_fallback_chain_length", description="hops walked before a request was served"
    )
    GATEWAY_STREAM_ERRORS = _meter.create_counter(
        "gateway_stream_errors_total",
        description="mid-stream deaths, zero vs nonzero tokens emitted",
    )
    GATEWAY_CACHE_HITS = _meter.create_counter(
        "gateway_cache_hits_total",
        description="cache hits by layer (gateway response / provider prefix)",
    )
    GATEWAY_CACHE_MISSES = _meter.create_counter(
        "gateway_cache_misses_total",
        description="cache misses by layer — hits alone flatter the meter",
    )
    GATEWAY_CACHE_TOKENS_SAVED = _meter.create_counter(
        "gateway_cache_tokens_saved", description="tokens the gateway response cache did not spend"
    )
    GATEWAY_COST = _meter.create_histogram(
        "gateway_cost_usd", description="estimated cost per request, by task class, backend, source"
    )
    GATEWAY_FALLBACK_SPEND = _meter.create_counter(
        "gateway_fallback_spend_usd", description="money the failure path spent falling forward"
    )
    _meter.create_observable_gauge(
        "gateway_queue_depth",
        callbacks=[_observe_gateway_depth],
        description="in-flight requests per backend — the leading pressure signal",
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
