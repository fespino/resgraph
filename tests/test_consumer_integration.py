"""Consumer integration tests — Redis stream in, Memgraph state out.

The delivery contract under test: at-least-once delivery (ack strictly
after apply) composed with the idempotent apply path yields exactly-once
store state. The crash test is the acceptance criterion from the phase
issue: kill between apply and ack, restart, no gap and no double-apply.

Needs both stores (docker compose up -d redis memgraph); skipped when
either is unreachable, forced by RESGRAPH_REQUIRE_STORES=1 in CI.
"""

import json
import os

import pytest

from resgraph.gen.churn import Churn
from resgraph.gen.sinks import RedisSink
from resgraph.gen.world import World
from resgraph.graph import ingest
from resgraph.graph.client import get_driver
from resgraph.graph.consumer import Consumer
from resgraph.graph.schema import init_schema
from resgraph.schema import Op, UpdateMessage

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("RESGRAPH_REDIS_URL", "redis://localhost:6379")
SEED, RESOURCES, CHURN_MESSAGES = 7, 60, 300


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


@pytest.fixture(scope="module")
def session():
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")
    with driver.session() as s:
        init_schema(s)
        yield s
    driver.close()


@pytest.fixture()
def stream(redis_client, request):
    """A fresh stream per test; deleting it also drops its groups."""
    name = f"resgraph:test:{request.node.name}"
    redis_client.delete(name)
    yield name
    redis_client.delete(name)


@pytest.fixture()
def clean_store(session):
    session.run("MATCH (n) DETACH DELETE n").consume()
    yield session


def _messages(n_churn: int = CHURN_MESSAGES) -> list[UpdateMessage]:
    churn = Churn(World(SEED, RESOURCES))
    msgs = list(churn.snapshot())
    msgs += [churn.next_message() for _ in range(n_churn)]
    return msgs


def _publish(stream: str, msgs: list[UpdateMessage]) -> None:
    sink = RedisSink(REDIS_URL, stream=stream)
    sink.emit_many(msgs)
    sink.close()


def _oracle(msgs: list[UpdateMessage]) -> dict[str, UpdateMessage]:
    """Expected store state = each resource's highest-sequence message.
    The stream is emitted in sequence order, so last-in wins."""
    last: dict[str, UpdateMessage] = {}
    for m in msgs:
        last[m.resource_id] = m
    return last


def _assert_store_matches(session, last: dict[str, UpdateMessage]) -> None:
    for rid, m in last.items():
        node = ingest.read_node(session, rid)
        assert node is not None, rid
        assert node["applied_seq"] == m.sequence, rid
        if m.op is Op.DELETE:
            assert node["deleted"] is True and node["attrs"] == {} and node["rels"] == []
        else:
            assert node["deleted"] is False, rid
            assert node["attrs"] == dict(m.attrs), rid
            expected_rels = sorted({(r.type.upper(), r.target_id) for r in m.relationships})
            assert node["rels"] == expected_rels, rid


def test_end_to_end_stream_to_store(redis_client, clean_store, stream):
    msgs = _messages()
    _publish(stream, msgs)
    consumer = Consumer(REDIS_URL, clean_store, stream=stream, group="g-e2e", name="c1")
    counters = consumer.run(exit_on_idle=True)
    consumer.close()
    assert counters["read"] == len(msgs)
    assert counters["applied"] == len(msgs)  # fresh store, in-order stream
    assert counters["invalid"] == 0
    _assert_store_matches(clean_store, _oracle(msgs))
    # nothing left unacknowledged
    assert redis_client.xpending(stream, "g-e2e")["pending"] == 0


def test_crash_between_apply_and_ack_no_gap_no_double_apply(redis_client, clean_store, stream):
    """The acceptance criterion: deliver a batch, apply part of it, crash
    before ANY ack. Restarting the same consumer must redeliver all of it,
    apply exactly the unapplied remainder, and leave nothing pending."""
    msgs = _messages(n_churn=40)
    _publish(stream, msgs)
    group, name = "g-crash", "c1"
    redis_client.xgroup_create(stream, group, id="0")

    # deliver everything to c1's pending list, then "crash": apply only
    # the first 30 messages, ack nothing.
    delivered = redis_client.xreadgroup(group, name, {stream: ">"}, count=len(msgs))
    assert len(delivered[0][1]) == len(msgs)
    for m in msgs[:30]:
        ingest.apply_message(clean_store, m)

    # restart: same consumer name resumes its own pending entries first
    consumer = Consumer(REDIS_URL, clean_store, stream=stream, group=group, name=name)
    counters = consumer.run(exit_on_idle=True)
    consumer.close()

    assert counters["read"] == len(msgs)  # everything redelivered
    assert counters["skipped"] == 30  # the watermark's scoreboard
    assert counters["applied"] == len(msgs) - 30
    assert redis_client.xpending(stream, group)["pending"] == 0
    _assert_store_matches(clean_store, _oracle(msgs))


def test_transport_replay_is_all_skipped(redis_client, clean_store, stream):
    """A full transport-level replay (same payloads, new entry ids) must
    change nothing: applied=0, state identical."""
    msgs = _messages(n_churn=40)
    _publish(stream, msgs)
    consumer = Consumer(REDIS_URL, clean_store, stream=stream, group="g-replay", name="c1")
    consumer.run(exit_on_idle=True)

    _publish(stream, msgs)  # the replay
    counters = consumer.run(exit_on_idle=True)
    consumer.close()
    assert counters["read"] == len(msgs)
    assert counters["applied"] == 0
    assert counters["skipped"] == len(msgs)
    _assert_store_matches(clean_store, _oracle(msgs))


def test_cli_ingest_end_to_end(redis_client, clean_store, stream):
    """The `resgraph ingest` command wires stream -> consumer -> store and
    reports the counters as JSON."""
    from typer.testing import CliRunner

    from resgraph.cli import app

    msgs = _messages(n_churn=20)
    _publish(stream, msgs)
    result = CliRunner().invoke(
        app,
        ["ingest", "--stream", stream, "--group", "g-cli", "--exit-on-idle"],
    )
    assert result.exit_code == 0, result.output
    counters = json.loads(result.output)
    assert counters["read"] == len(msgs)
    assert counters["applied"] == len(msgs)
    assert redis_client.xpending(stream, "g-cli")["pending"] == 0


def test_poison_entry_is_dropped_not_wedged(redis_client, clean_store, stream):
    """An unparseable payload is counted, acked, and skipped — it must not
    block the entries behind it."""
    msgs = _messages(n_churn=0)
    good = msgs[0]
    redis_client.xadd(stream, {"data": b"{not json"})
    redis_client.xadd(stream, {"data": json.dumps({"schema_version": 99}).encode()})
    redis_client.xadd(stream, {"data": good.model_dump_json().encode()})

    consumer = Consumer(REDIS_URL, clean_store, stream=stream, group="g-poison", name="c1")
    counters = consumer.run(exit_on_idle=True)
    consumer.close()
    assert counters == {"read": 3, "applied": 1, "skipped": 0, "invalid": 2}
    assert redis_client.xpending(stream, "g-poison")["pending"] == 0
    assert ingest.read_node(clean_store, good.resource_id) is not None
