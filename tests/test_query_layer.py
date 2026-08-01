"""Query-layer tests: assert PLANS, not just results (D16).

Placement and laziness are the phase's contract, so the suite pins the
plan shape directly — where each predicate went, that residuals are
visible, and that explain never opens a store. The composite path runs
against the in-process cold store and the generator oracle; only the
hot/cold boundary golden test needs Memgraph (test_query_integration).
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from resgraph.api import app as api_app
from resgraph.cold import queries as cold_queries
from resgraph.cold import store as cold_store
from resgraph.gen.churn import Churn
from resgraph.gen.world import ATTR_POOLS, World
from resgraph.query.dsl import Predicate, parse_filter
from resgraph.query.executor import QueryContext, _residual_filter, execute_plan
from resgraph.query.planner import KNOWN_FIELDS, Query, place, plan
from resgraph.schema import Op

SEED, RESOURCES, CHURN = 42, 100, 400
T_LIVE = datetime(2026, 1, 3, tzinfo=UTC)


def _messages():
    churn = Churn(World(SEED, RESOURCES))
    return list(churn.snapshot()) + [churn.next_message() for _ in range(CHURN)]


MSGS = _messages()
T_END = MSGS[-1].event_time


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    cat = cold_store.get_catalog(tmp_path_factory.mktemp("query-cold"))
    cold_store.ensure_tables(cat)
    cold_store.append_events(cat, MSGS)
    return cat


# --- DSL ----------------------------------------------------------------


def test_parse_conjunction_with_coercion():
    preds = parse_filter("type=vm AND attrs.cpu>=4 AND attrs.zone=z1")
    assert preds == [
        Predicate("type", "=", "vm"),
        Predicate("attrs.cpu", ">=", 4),
        Predicate("attrs.zone", "=", "z1"),
    ]


def test_parse_bool_and_float():
    assert parse_filter("attrs.open_to_world=true")[0].value is True
    assert parse_filter("attrs.load>0.5")[0].value == 0.5


@pytest.mark.parametrize(
    "bad",
    [
        "type=vm OR type=db",
        "(type=vm)",
        "type=vm AND",
        "attrs.zone LIKE z%",
        "attrs.zone>z1",
    ],
)
def test_grammar_rejects_whats_outside_d16(bad):
    with pytest.raises(ValueError):
        parse_filter(bad)


def test_empty_filter_is_no_predicates():
    assert parse_filter(None) == [] and parse_filter("  ") == []


# --- placement + plan shape ----------------------------------------------


def test_known_fields_derive_from_generator_pools():
    assert "attrs.zone" in KNOWN_FIELDS
    assert "attrs.open_to_world" in KNOWN_FIELDS
    assert "type" in KNOWN_FIELDS
    assert "attrs.frobnitz" not in KNOWN_FIELDS


def test_zone_predicate_pushes_to_cold_in_asof_query():
    p = plan(
        Query(
            "blast_radius",
            root="vm-000001",
            at=T_LIVE,
            predicates=parse_filter("attrs.zone=z1"),
        )
    )
    cold = next(s for s in p.steps if s.store == "cold")
    assert "zone" in " ".join(cold.pushed) and not p.residual


def test_unknown_attr_becomes_visible_residual():
    p = plan(Query("blast_radius", root="vm-000001", predicates=parse_filter("attrs.frobnitz=9")))
    assert [str(r) for r in p.residual] == ["attrs.frobnitz=9"]
    assert any(s.op == "residual_filter" for s in p.steps)


def test_live_route_pushes_into_cypher():
    p = plan(
        Query("blast_radius", root="vm-000001", predicates=parse_filter("type=vm AND attrs.cpu>=4"))
    )
    (hot,) = [s for s in p.steps if s.store == "hot"]
    assert "dep:vm" in hot.detail and "dep.cpu >=" in hot.detail
    assert not p.residual


def test_composite_evaluates_predicates_as_match_column_not_scan_reduction():
    p = plan(
        Query("blast_radius", root="vm-000001", at=T_LIVE, predicates=parse_filter("attrs.zone=z1"))
    )
    cold = next(s for s in p.steps if s.store == "cold")
    assert "matched :=" in cold.detail and "WHERE" not in cold.detail
    assert [s.store for s in p.steps] == ["cold", "python"]


def test_world_filter_is_a_scan_reduction():
    p = plan(Query("world", at=T_LIVE, predicates=parse_filter("attrs.zone=z1")))
    (cold,) = p.steps
    assert "WHERE" in cold.detail


def test_plan_rejects_unknown_type_and_type_ordering():
    with pytest.raises(ValueError):
        plan(Query("blast_radius", root="x-1", predicates=parse_filter("type=frobnitz")))
    with pytest.raises(ValueError):
        place(parse_filter("type<=vm"))


def test_live_world_is_not_an_endpoint():
    with pytest.raises(ValueError):
        plan(Query("world"))


# --- planner properties (random inputs, invariants hold) -----------------

_FIELDS = st.sampled_from(
    sorted(KNOWN_FIELDS - {"type"}) + ["attrs.frobnitz", "size", "attrs.made_up"]
)
_PREDS = st.one_of(
    st.builds(
        Predicate,
        _FIELDS,
        st.sampled_from(["=", "!="]),
        st.one_of(st.integers(-100, 100), st.sampled_from(["z1", "up", "r3"])),
    ),
    st.builds(Predicate, _FIELDS, st.sampled_from(["<", "<=", ">", ">="]), st.integers(-100, 100)),
)


@given(preds=st.lists(_PREDS, max_size=6))
def test_placement_partitions_predicates_exactly(preds):
    claimable, residual = place(preds)
    assert sorted(map(str, claimable + residual)) == sorted(map(str, preds))
    assert all(p.field in KNOWN_FIELDS for p in claimable)
    assert all(p.field not in KNOWN_FIELDS for p in residual)


_KNOWN_ATTR_VALUES = sorted(
    {
        (f"attrs.{key}", value)
        for pools in ATTR_POOLS.values()
        for key, values in pools.items()
        for value in values
    },
    key=str,
)


@settings(max_examples=15, deadline=None)
@given(choice=st.sampled_from(_KNOWN_ATTR_VALUES), op=st.sampled_from(["=", "!="]))
def test_pushed_equals_forced_residual(catalog, choice, op):
    """The optimized-vs-unoptimized comparison: pushing a predicate into
    the DuckDB scan and filtering the full world in Python must agree
    for any generator-known attr and value."""
    field, value = choice
    pred = Predicate(field, op, value)
    ctx = QueryContext(catalog=catalog)
    pushed = execute_plan(plan(Query("world", at=T_END, predicates=[pred])), ctx)
    full = execute_plan(plan(Query("world", at=T_END)), ctx)
    forced = _residual_filter(full, [pred])
    assert {r["id"] for r in pushed} == {r["id"] for r in forced}


# --- execution against the cold store + oracle ---------------------------


def _oracle_state(t):
    last = {}
    for m in MSGS:
        if m.event_time <= t and (
            m.resource_id not in last or m.sequence > last[m.resource_id].sequence
        ):
            last[m.resource_id] = m
    return {rid: m for rid, m in last.items() if m.op is Op.UPSERT}


def _oracle_blast(t, root, depth):
    state = _oracle_state(t)
    dependents = {}
    for m in state.values():
        for rel in m.relationships:
            dependents.setdefault(rel.target_id, set()).add(m.resource_id)
    seen, frontier = {root}, [root]
    for _ in range(depth):
        frontier = [d for n in frontier for d in dependents.get(n, ()) if d not in seen]
        seen.update(frontier)
    seen.discard(root)
    return seen


def test_composite_blast_radius_matches_oracle(catalog):
    ctx = QueryContext(catalog=catalog)
    state = _oracle_state(T_END)
    roots = sorted(state)[:5] + sorted(r for r in state if r.startswith("host"))[:2]
    for root in roots:
        got = execute_plan(plan(Query("blast_radius", root=root, at=T_END)), ctx)
        assert {r["id"] for r in got} == _oracle_blast(T_END, root, 3), root


def test_composite_filter_selects_within_the_affected_set(catalog):
    ctx = QueryContext(catalog=catalog)
    state = _oracle_state(T_END)
    root = next(r for r in sorted(state) if _oracle_blast(T_END, r, 3))
    unfiltered = {
        r["id"] for r in execute_plan(plan(Query("blast_radius", root=root, at=T_END)), ctx)
    }
    filtered = execute_plan(
        plan(Query("blast_radius", root=root, at=T_END, predicates=parse_filter("type=vm"))),
        ctx,
    )
    assert {r["id"] for r in filtered} <= unfiltered
    assert all(r["type"] == "vm" for r in filtered)
    assert {r["id"] for r in filtered} == {i for i in unfiltered if i.startswith("vm")}


def test_residual_filters_after_traverse(catalog):
    ctx = QueryContext(catalog=catalog)
    state = _oracle_state(T_END)
    root = next(r for r in sorted(state) if _oracle_blast(T_END, r, 3))
    got = execute_plan(
        plan(
            Query("blast_radius", root=root, at=T_END, predicates=parse_filter("attrs.frobnitz=9"))
        ),
        ctx,
    )
    assert got == []


def test_world_pushdown_equals_oracle_filter(catalog):
    ctx = QueryContext(catalog=catalog)
    got = execute_plan(
        plan(Query("world", at=T_END, predicates=parse_filter("attrs.zone=z1"))), ctx
    )
    expected = {rid for rid, m in _oracle_state(T_END).items() if m.attrs.get("zone") == "z1"}
    assert {r["id"] for r in got} == expected
    assert all(r["attrs"]["zone"] == "z1" for r in got)


def test_numeric_pushdown_on_json_attr(catalog):
    ctx = QueryContext(catalog=catalog)
    got = execute_plan(plan(Query("world", at=T_END, predicates=parse_filter("attrs.cpu>=4"))), ctx)
    expected = {
        rid
        for rid, m in _oracle_state(T_END).items()
        if isinstance(m.attrs.get("cpu"), int) and m.attrs["cpu"] >= 4
    }
    assert {r["id"] for r in got} == expected


def test_missing_store_is_a_named_error():
    with pytest.raises(RuntimeError, match="hot store"):
        execute_plan(plan(Query("blast_radius", root="vm-000001")), QueryContext())


# --- the API surface (D15) ------------------------------------------------


@pytest.fixture()
def client(catalog):
    api_app.app.dependency_overrides[api_app.get_ctx] = lambda: QueryContext(catalog=catalog)
    yield TestClient(api_app.app)
    api_app.app.dependency_overrides.clear()


def test_explain_answers_without_any_store():
    # no stores are running; without explain this request would fail —
    # with it, it must not even try
    client = TestClient(api_app.app)
    r = client.get(
        "/blast-radius/vm-000001",
        params={
            "at": T_LIVE.isoformat(),
            "filter": "attrs.zone=z1 AND attrs.frobnitz=9",
            "explain": "true",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "plan"
    stores = [s["store"] for s in body["plan"]["steps"]]
    assert stores == ["cold", "python", "python"]
    assert body["plan"]["residual"] == ["attrs.frobnitz=9"]
    assert api_app.app.state.driver is None and api_app.app.state.catalog is None


def test_world_endpoint_labels_and_budgets(client):
    r = client.get("/world", params={"at": T_END.isoformat(), "filter": "attrs.zone=z1"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "cold" and "fetched_at" in body
    assert body["truncated"] is False
    assert body["total_count"] == len(body["resources"])


def test_budget_caps_and_flags(client, monkeypatch):
    monkeypatch.setattr(api_app, "MAX_ROWS", 5)
    r = client.get("/world", params={"at": T_END.isoformat()})
    body = r.json()
    assert len(body["resources"]) == 5
    assert body["truncated"] is True
    assert body["total_count"] > 5


def test_composite_endpoint_source_is_composite(client):
    state = _oracle_state(T_END)
    root = next(r for r in sorted(state) if _oracle_blast(T_END, r, 3))
    r = client.get(f"/blast-radius/{root}", params={"at": T_END.isoformat()})
    body = r.json()
    assert body["source"] == "composite"
    assert {a["id"] for a in body["affected"]} == _oracle_blast(T_END, root, 3)


def test_history_endpoint_matches_cold(client, catalog):
    rid = MSGS[-1].resource_id
    r = client.get(f"/history/{rid}")
    body = r.json()
    assert body["source"] == "cold"
    assert [e["sequence"] for e in body["events"]] == [
        h["sequence"] for h in cold_queries.history(catalog, rid, limit=1001)
    ]


def test_diff_endpoint(client):
    t1 = MSGS[len(MSGS) // 2].event_time
    r = client.get("/diff", params={"from": t1.isoformat(), "to": T_END.isoformat()})
    body = r.json()
    assert body["source"] == "cold"
    assert set(body["total_count"]) == {"created", "deleted", "changed"}


@pytest.mark.parametrize(
    "params",
    [
        {"filter": "type=vm OR type=db"},
        {"filter": "type=frobnitz"},
        {"at": "2026-01-03T00:00:00"},
    ],
)
def test_bad_requests_are_400(client, params):
    r = client.get("/blast-radius/vm-000001", params={"at": T_END.isoformat(), **params})
    assert r.status_code == 400
