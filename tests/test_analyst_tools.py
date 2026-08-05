"""RegistryToolset dispatch (#130): the error paths a paid run rides
when things go wrong — unknown tool, invalid input, store failure —
and the read-only refusal that makes the surface safe by
construction, not by prompt."""

import json
from types import SimpleNamespace

import pytest

from resgraph.analyst.tools import RegistryToolset
from resgraph.query.executor import QueryContext


def toolset(session_factory=None):
    return RegistryToolset(lambda: QueryContext(session_factory=session_factory))


def test_unknown_tool_is_invalid_input():
    outcome = toolset().execute("not_a_tool", {})
    assert not outcome.ok
    payload = json.loads(outcome.payload)
    assert payload["error_class"] == "invalid_input"
    assert "unknown tool" in payload["message"]


def test_bad_args_fail_validation_with_field_detail():
    outcome = toolset().execute("fetch_resource", {})
    assert not outcome.ok
    payload = json.loads(outcome.payload)
    assert payload["error_class"] == "invalid_input"
    assert "resource_id" in payload["message"]


def test_store_failure_maps_to_store_unavailable():
    def broken_session():
        raise ConnectionError("store down")

    outcome = toolset(broken_session).execute("fetch_resource", {"resource_id": "host-000001"})
    assert not outcome.ok
    payload = json.loads(outcome.payload)
    assert payload["error_class"] == "store_unavailable"
    assert "store down" in payload["message"]


def test_non_read_only_tool_is_refused_at_construction():
    write_tool = SimpleNamespace(
        name="mutate_things", hints=SimpleNamespace(read_only=False), privileged=False
    )
    with pytest.raises(RuntimeError, match="refuses"):
        RegistryToolset(QueryContext, entries=(write_tool,))


def test_lazy_context_caches_driver_and_catalog(monkeypatch):
    from resgraph.analyst import tools as mod

    created = []
    monkeypatch.setattr(mod, "_driver", None)
    monkeypatch.setattr(mod, "_catalog", None)
    monkeypatch.setattr(
        "resgraph.graph.client.get_driver",
        lambda: created.append("driver") or SimpleNamespace(session=lambda: "SESSION"),
    )
    monkeypatch.setattr(
        "resgraph.cold.store.get_catalog", lambda: created.append("catalog") or "CAT"
    )

    qctx = mod._query_context()
    assert qctx.session_factory() == "SESSION"
    assert qctx.session_factory() == "SESSION"
    assert qctx.catalog_factory() == "CAT"
    assert created == ["driver", "catalog"]
    assert mod.default_toolset() is not None


@pytest.mark.integration
def test_success_path_returns_payload_and_closes_session():
    import os

    from resgraph.graph.client import get_driver

    try:
        driver = get_driver()
        driver.verify_connectivity()
    except Exception:
        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable (docker compose up -d)")
    ts = RegistryToolset(lambda: QueryContext(session_factory=driver.session))
    outcome = ts.execute("fetch_resource", {"resource_id": "host-000001"})
    assert outcome.ok
    json.loads(outcome.payload)
    driver.close()
