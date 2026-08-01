"""Cold-store tests — event-time travel against the world oracle (D11–D13).

Everything here runs in-process (filesystem warehouse, DuckDB reads);
only the stream-consumer test needs redis and carries the integration
marker. Determinism (D6) makes the oracle exact: for any T, the
expected state is computable from the message list alone.
"""

import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from resgraph.cold import queries, store
from resgraph.gen.churn import Churn
from resgraph.gen.world import World
from resgraph.schema import Op, UpdateMessage

SEED, RESOURCES, CHURN = 42, 100, 400


def _messages() -> list[UpdateMessage]:
    churn = Churn(World(SEED, RESOURCES))
    return list(churn.snapshot()) + [churn.next_message() for _ in range(CHURN)]


def _oracle(msgs: list[UpdateMessage], t) -> dict[str, UpdateMessage]:
    """Alive state at t: highest-sequence message per resource with
    event_time <= t, deletes dropped."""
    last: dict[str, UpdateMessage] = {}
    for m in msgs:
        if m.event_time <= t and (
            m.resource_id not in last or m.sequence > last[m.resource_id].sequence
        ):
            last[m.resource_id] = m
    return {rid: m for rid, m in last.items() if m.op is Op.UPSERT}


def _assert_state_matches(rows: list[dict], expected: dict[str, UpdateMessage]) -> None:
    got = {r["resource_id"]: r for r in rows}
    assert set(got) == set(expected)
    for rid, m in expected.items():
        assert got[rid]["attrs"] == dict(m.attrs), rid
        assert got[rid]["sequence"] == m.sequence, rid
        assert got[rid]["resource_type"] == m.resource_type.value, rid


MSGS = _messages()
_TIMES = st.integers(min_value=0, max_value=len(MSGS) - 1)


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    cat = store.get_catalog(tmp_path_factory.mktemp("cold"))
    store.ensure_tables(cat)
    store.append_events(cat, MSGS)
    # snapshots at 1/3 and 2/3 of the run — the accelerated path has
    # real checkpoints to start from
    queries.snapshot_at(cat, MSGS[len(MSGS) // 3].event_time)
    queries.snapshot_at(cat, MSGS[2 * len(MSGS) // 3].event_time)
    return cat


@settings(max_examples=15, deadline=None)
@given(i=_TIMES)
def test_state_at_matches_oracle_at_any_time(catalog, i):
    t = MSGS[i].event_time
    _assert_state_matches(queries.state_at(catalog, t, use_snapshots=False), _oracle(MSGS, t))


@settings(max_examples=15, deadline=None)
@given(i=_TIMES)
def test_snapshot_accelerated_equals_pure_replay(catalog, i):
    t = MSGS[i].event_time
    accel = queries.state_at(catalog, t, use_snapshots=True)
    pure = queries.state_at(catalog, t, use_snapshots=False)
    assert accel == pure


def test_duplicate_append_changes_no_answer(tmp_path):
    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    store.append_events(cat, MSGS)
    t = MSGS[len(MSGS) // 2].event_time
    before = queries.state_at(cat, t)
    store.append_events(cat, MSGS)  # full transport-level replay
    store.append_events(cat, MSGS[:100])  # and a partial one
    assert queries.state_at(cat, t) == before


def test_snapshot_built_from_snapshot_stays_correct(tmp_path):
    # Guards the watermark-as-sequence design bug: a snapshot derived
    # from an earlier snapshot must still carry per-resource sequences.
    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    store.append_events(cat, MSGS)
    t1 = MSGS[len(MSGS) // 4].event_time
    t2 = MSGS[len(MSGS) // 2].event_time
    t3 = MSGS[-1].event_time
    queries.snapshot_at(cat, t1)
    queries.snapshot_at(cat, t2)  # built via t1's snapshot
    _assert_state_matches(queries.state_at(cat, t3), _oracle(MSGS, t3))


def test_diff_between_two_times(tmp_path):
    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    store.append_events(cat, MSGS)
    t1 = MSGS[len(MSGS) // 2].event_time
    t2 = MSGS[-1].event_time
    a, b = _oracle(MSGS, t1), _oracle(MSGS, t2)
    d = queries.diff(cat, t1, t2)
    assert d["created"] == sorted(b.keys() - a.keys())
    assert d["deleted"] == sorted(a.keys() - b.keys())


def test_history_is_deduped_and_ordered(tmp_path):
    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    store.append_events(cat, MSGS)
    store.append_events(cat, MSGS)  # duplicates must not double history
    rid = MSGS[-1].resource_id
    expected = sorted({m.sequence for m in MSGS if m.resource_id == rid})
    got = [h["sequence"] for h in queries.history(cat, rid, limit=1000)]
    assert got == expected


def test_ensure_tables_idempotent(tmp_path):
    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    store.ensure_tables(cat)
    assert cat.table_exists(store.EVENTS) and cat.table_exists(store.SNAPSHOTS)


# --- the stream consumer, under crash/redelivery (needs redis) ----------


@pytest.mark.integration
def test_cold_consumer_crash_causes_duplicates_not_wrong_answers(tmp_path):
    """The cold twist on the phase-3 crash test: redelivery after a crash
    produces duplicate ROWS (appends aren't idempotent) — and identical
    ANSWERS (reads dedupe, D12)."""
    import redis as redis_lib

    from resgraph.consumer import StreamConsumer
    from resgraph.gen.sinks import RedisSink

    url = os.environ.get("RESGRAPH_REDIS_URL", "redis://localhost:6379")
    try:
        r = redis_lib.Redis.from_url(url)
        r.ping()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("redis not reachable (docker compose up -d redis)")

    stream = "resgraph:test:cold-crash"
    r.delete(stream)
    sink = RedisSink(url, stream=stream)
    sink.emit_many(MSGS)
    sink.close()

    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)

    def apply(msgs):
        return (store.append_events(cat, msgs), 0)

    group, name = "g-cold-crash", "c1"
    r.xgroup_create(stream, group, id="0")
    # deliver a first chunk to the PEL, append it, then "crash" before ack
    delivered = r.xreadgroup(group, name, {stream: ">"}, count=200)
    crashed_batch = [
        UpdateMessage.model_validate_json(fields[b"data"]) for _, fields in delivered[0][1]
    ]
    store.append_events(cat, crashed_batch)

    # restart: pending redelivered (duplicate appends), then the rest
    consumer = StreamConsumer(url, apply, stream=stream, group=group, name=name)
    counters = consumer.run(exit_on_idle=True)
    consumer.close()

    assert counters["read"] == len(MSGS)
    assert r.xpending(stream, group)["pending"] == 0
    r.delete(stream)
    total_rows = cat.load_table(store.EVENTS).scan().to_arrow().num_rows
    assert total_rows == len(MSGS) + len(crashed_batch)  # duplicates exist...
    t = MSGS[-1].event_time
    _assert_state_matches(queries.state_at(cat, t), _oracle(MSGS, t))  # ...answers don't


# --- rebuild-from-cold: the DR drill (needs memgraph) --------------------


@pytest.mark.integration
def test_rebuild_from_cold_restores_state_and_watermarks(tmp_path):
    """Kill the hot store, rebuild from the log, and prove three things:
    state matches the oracle, the watermarks survived (stale replay
    skips), and post-rebuild ingest of newer events applies normally."""
    from resgraph.cold.rebuild import rebuild
    from resgraph.graph import ingest
    from resgraph.graph.client import get_driver
    from resgraph.graph.schema import wipe

    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d memgraph)")

    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    cut = len(MSGS) - 50  # rebuild at T, with 50 newer events still in the log
    t = MSGS[cut - 1].event_time
    store.append_events(cat, MSGS)

    expected = _oracle(MSGS, t)
    with driver.session() as s:
        wipe(s)  # the disaster
        result = rebuild(s, cat, t)
        assert result["nodes"] == len(expected)

        # state matches the oracle, watermark included
        for rid, m in expected.items():
            node = ingest.read_node(s, rid)
            assert node is not None and node["attrs"] == dict(m.attrs), rid
            assert node["applied_seq"] == m.sequence, rid

        # stale replay is a no-op: every pre-T message skips
        for m in MSGS[: cut - 1 : 37]:  # a stride of old messages
            assert ingest.apply_message(s, m) is False, m.sequence

        # the resurrection guard: a resource dead at T must keep its
        # tombstone watermark, so its own OLD upserts skip too
        dead = {d["resource_id"]: d for d in queries.tombstones_at(cat, t)}
        assert dead, "fixture must contain at least one pre-T deletion"
        revived = 0
        for m in MSGS[: cut - 1]:
            if m.resource_id in dead and m.op is Op.UPSERT:
                assert ingest.apply_message(s, m) is False, m.resource_id
                revived += 1
        assert revived > 0  # the guard was actually exercised

        # the stream tail applies normally on resume
        applied, _ = ingest.apply_batch(s, MSGS[cut:])
        assert applied > 0
        t_end = MSGS[-1].event_time
        final = _oracle(MSGS, t_end)
        # same world means same world: alive set, attrs, and watermarks
        got = {r["resource_id"] for r in queries.state_at(cat, t_end)}
        alive_hot = {rid for rid in got if (n := ingest.read_node(s, rid)) and not n["deleted"]}
        assert alive_hot == set(final)
        for rid, m in final.items():
            node = ingest.read_node(s, rid)
            assert node["attrs"] == dict(m.attrs), rid
            assert node["applied_seq"] == m.sequence, rid
    driver.close()


def test_maintain_expires_snapshots_but_keeps_data(tmp_path):
    cat = store.get_catalog(tmp_path)
    store.ensure_tables(cat)
    for i in range(0, 300, 100):  # three commits = three snapshots
        store.append_events(cat, MSGS[i : i + 100])
    rows_before = cat.load_table(store.EVENTS).scan().to_arrow().num_rows
    result = store.maintain(cat, tmp_path)
    assert result[store.EVENTS]["snapshots_before"] == 3
    assert result[store.EVENTS]["snapshots_after"] == 1
    assert cat.load_table(store.EVENTS).scan().to_arrow().num_rows == rows_before
