"""Apply-failure containment (D14 addendum, #44) — redis only, no graph.

The three mechanisms under test: retry-then-split isolates a
deterministic apply poison into the DLQ while its siblings apply; a
transient failure is retried and leaves no trace; a crash-looped entry
(delivery count over the cap) is quarantined before apply ever sees it.
"""

import json
import os

import pytest

from resgraph.consumer import StreamConsumer
from resgraph.gen.churn import Churn
from resgraph.gen.world import World

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("RESGRAPH_REDIS_URL", "redis://localhost:6379")


@pytest.fixture(scope="module")
def redis_client():
    r = None
    try:
        import redis

        r = redis.Redis.from_url(REDIS_URL)
        r.ping()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("redis not reachable (docker compose up -d redis)")
    yield r
    r.close()


@pytest.fixture()
def stream(redis_client, request):
    name = f"resgraph:test:{request.node.name}"
    redis_client.delete(name, f"{name}:dlq")
    yield name
    redis_client.delete(name, f"{name}:dlq")


def _messages(n_churn: int = 40):
    churn = Churn(World(11, 30))
    return list(churn.snapshot()) + [churn.next_message() for _ in range(n_churn)]


def _fill(redis_client, stream, msgs):
    for m in msgs:
        redis_client.xadd(stream, {"data": m.model_dump_json()})


def test_apply_poison_lands_in_dlq_and_siblings_apply(redis_client, stream):
    msgs = _messages()
    poison = (msgs[7].resource_id, msgs[7].sequence)
    _fill(redis_client, stream, msgs)
    applied = []

    def apply(batch):
        if any((m.resource_id, m.sequence) == poison for m in batch):
            raise RuntimeError("store rejected the batch")
        applied.extend((m.resource_id, m.sequence) for m in batch)
        return (len(batch), 0)

    consumer = StreamConsumer(
        REDIS_URL,
        apply,
        stream=stream,
        group="g-dlq",
        name="c1",
        batch=16,
        block_ms=100,
        backoff_s=0,
    )
    counters = consumer.run(exit_on_idle=True)
    consumer.close()

    assert counters["dead_lettered"] == 1
    assert counters["applied"] == len(msgs) - 1
    survivors = [m for m in msgs if (m.resource_id, m.sequence) != poison]
    expected = sorted((m.resource_id, m.sequence) for m in survivors)
    assert sorted(applied) == expected

    dlq = redis_client.xrange(f"{stream}:dlq")
    assert len(dlq) == 1
    fields = dlq[0][1]
    assert b"store rejected" in fields[b"error"]
    assert int(fields[b"deliveries"]) >= 1
    assert json.loads(fields[b"data"])["resource_id"] == poison[0]
    assert redis_client.xpending(stream, "g-dlq")["pending"] == 0


def test_transient_failure_is_retried_and_leaves_no_trace(redis_client, stream):
    msgs = _messages(10)
    _fill(redis_client, stream, msgs)
    calls = {"n": 0}

    def apply(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient blip")
        return (len(batch), 0)

    consumer = StreamConsumer(
        REDIS_URL,
        apply,
        stream=stream,
        group="g-flaky",
        name="c1",
        block_ms=100,
        backoff_s=0,
    )
    counters = consumer.run(exit_on_idle=True)
    consumer.close()

    assert counters["dead_lettered"] == 0
    assert counters["applied"] == len(msgs)
    assert calls["n"] == 2
    assert not redis_client.exists(f"{stream}:dlq")
    assert redis_client.xpending(stream, "g-flaky")["pending"] == 0


def test_crash_looped_entries_quarantined_by_delivery_cap(redis_client, stream):
    msgs = _messages(0)[:3]
    _fill(redis_client, stream, msgs)
    group, name = "g-cap", "c1"
    redis_client.xgroup_create(stream, group, id="0", mkstream=True)
    redis_client.xreadgroup(group, name, {stream: ">"}, count=10)
    for _ in range(5):
        redis_client.xreadgroup(group, name, {stream: "0"}, count=10)

    seen = []

    def apply(batch):
        seen.extend(batch)
        return (len(batch), 0)

    consumer = StreamConsumer(
        REDIS_URL,
        apply,
        stream=stream,
        group=group,
        name=name,
        block_ms=100,
        backoff_s=0,
    )
    counters = consumer.run(exit_on_idle=True)
    consumer.close()

    assert counters["dead_lettered"] == 3
    assert counters["applied"] == 0 and seen == []
    dlq = redis_client.xrange(f"{stream}:dlq")
    assert len(dlq) == 3
    assert all(int(fields[b"deliveries"]) > 5 for _, fields in dlq)
    assert redis_client.xpending(stream, group)["pending"] == 0


def test_store_outage_under_load_keeps_dlq_flat(redis_client, stream):
    """The D14-supersession acceptance test, time-compressed: a
    real outage runs to minutes; here the 'store' rejects
    every apply for the first second of wall time, then recovers. Under
    the pre-supersession behavior every in-flight entry would walk
    retry -> split -> DLQ; now the DLQ must stay flat and everything
    applies after recovery."""
    import time as _time

    class StoreDown(Exception):
        pass

    msgs = _messages(60)
    _fill(redis_client, stream, msgs)
    deadline = _time.monotonic() + 1.0
    applied = []

    def apply(batch):
        if _time.monotonic() < deadline:
            raise StoreDown("connection refused")
        applied.extend(batch)
        return (len(batch), 0)

    consumer = StreamConsumer(
        REDIS_URL,
        apply,
        stream=stream,
        group="g-outage",
        name="c1",
        block_ms=100,
        backoff_s=0.05,
        retryable_exceptions=(StoreDown,),
    )
    counters = consumer.run(exit_on_idle=True)
    consumer.close()

    assert counters["dead_lettered"] == 0
    assert counters["applied"] == len(msgs)
    assert not redis_client.exists(f"{stream}:dlq")
    assert redis_client.xpending(stream, "g-outage")["pending"] == 0
