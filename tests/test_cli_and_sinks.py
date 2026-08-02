"""Coverage for the CLI surfaces and sinks — the parts that ship a
user-facing command but never ran in a test."""

import json
import sys
import types

import pytest
from typer.testing import CliRunner

from resgraph.gen.cli import app as gen_app
from resgraph.gen.sinks import RedisSink, StdoutSink
from resgraph.schema import Op, UpdateMessage

runner = CliRunner()


def _msg(seq: int = 1) -> UpdateMessage:
    return UpdateMessage(
        sequence=seq,
        event_time="2026-01-01T00:00:00Z",
        op=Op.UPSERT,
        resource_type="vm",
        resource_id=f"vm-{seq}",
    )


# --- sinks -----------------------------------------------------------------


def test_stdout_sink_emits_jsonl(capsys):
    StdoutSink().emit_many([_msg(1), _msg(2)])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    assert json.loads(out[0])["resource_id"] == "vm-1"


def test_redis_sink_pipelines(monkeypatch):
    calls: dict = {"xadd": 0, "execute": 0, "closed": False}

    class FakePipe:
        def xadd(self, *a, **k):
            calls["xadd"] += 1

        def execute(self):
            calls["execute"] += 1

    class FakeRedis:
        @classmethod
        def from_url(cls, url):
            return cls()

        def pipeline(self, transaction=False):
            return FakePipe()

        def close(self):
            calls["closed"] = True

    monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(Redis=FakeRedis))
    sink = RedisSink("redis://x")
    sink.emit_many([_msg(1), _msg(2), _msg(3)])
    sink.close()
    assert calls == {"xadd": 3, "execute": 1, "closed": True}


# --- generator CLI ---------------------------------------------------------


def test_gen_run_stdout_deterministic():
    a = runner.invoke(gen_app, ["run", "--seed", "7", "--resources", "60", "--count", "40"])
    b = runner.invoke(gen_app, ["run", "--seed", "7", "--resources", "60", "--count", "40"])
    assert a.exit_code == 0 and a.stdout == b.stdout
    assert len(a.stdout.strip().splitlines()) == 40


def test_gen_seed_snapshot_count():
    r = runner.invoke(gen_app, ["seed", "--seed", "7", "--resources", "60"])
    assert r.exit_code == 0
    lines = r.stdout.strip().splitlines()
    assert all(json.loads(line)["op"] == "upsert" for line in lines)


def test_gen_run_rejects_unknown_sink():
    r = runner.invoke(gen_app, ["run", "--sink", "carrier-pigeon", "--count", "1"])
    assert r.exit_code != 0


def test_gen_run_duration_bounded(monkeypatch):
    # duration path: monotonic jumps past the limit after the first batch
    ticks = iter([0.0, 999.0, 999.0])
    monkeypatch.setattr("resgraph.gen.cli.time.monotonic", lambda: next(ticks, 999.0))
    r = runner.invoke(
        gen_app, ["run", "--seed", "1", "--resources", "60", "--duration", "1", "--batch", "10"]
    )
    assert r.exit_code == 0
    assert len(r.stdout.strip().splitlines()) == 10  # exactly one batch, then time's up


# --- platform CLI (needs the store) ---------------------------------------


@pytest.mark.integration
def test_platform_cli_end_to_end():
    from resgraph.cli import app as plat_app
    from resgraph.graph.client import get_driver
    from resgraph.graph.schema import wipe

    try:
        d = get_driver()
        d.verify_connectivity()
    except Exception:
        import os

        if os.environ.get("RESGRAPH_REQUIRE_STORES"):
            raise
        pytest.skip("memgraph not reachable")
    with d.session() as s:
        wipe(s)
    d.close()

    snap = runner.invoke(gen_app, ["seed", "--seed", "9", "--resources", "80"])
    load = runner.invoke(plat_app, ["load-snapshot"], input=snap.stdout)
    assert load.exit_code == 0 and json.loads(load.stdout)["nodes"] > 0

    stats = runner.invoke(plat_app, ["query", "stats"])
    assert stats.exit_code == 0 and json.loads(stats.stdout)["nodes"]

    orphans = runner.invoke(plat_app, ["query", "orphans"])
    assert orphans.exit_code == 0

    # host-000001 exists — hosts are created first (D5 seeding order)
    br = runner.invoke(plat_app, ["query", "blast-radius", "--id", "host-000001", "--depth", "2"])
    assert br.exit_code == 0
    path = runner.invoke(
        plat_app, ["query", "path", "--from", "host-000001", "--to", "host-000001"]
    )
    assert path.exit_code == 0


# --- surfaces the suite never walked (#65) -------------------------------


def test_gen_final_state_prints_alive_world():
    res = runner.invoke(
        gen_app, ["final-state", "--seed", "7", "--resources", "20", "--count", "30"]
    )
    assert res.exit_code == 0
    rows = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
    assert rows and len({r["resource_id"] for r in rows}) == len(rows)
    assert all("sequence" in r and "relationships" in r for r in rows)


def test_gen_run_throttled_emits_exact_count():
    res = runner.invoke(
        gen_app, ["run", "--seed", "7", "--resources", "20", "--count", "40", "--rate", "5000"]
    )
    assert res.exit_code == 0
    assert len([ln for ln in res.stdout.splitlines() if ln.strip()]) == 40


def test_gen_seed_routes_through_the_redis_sink(monkeypatch):
    import resgraph.gen.cli as gen_cli_mod

    emitted = []

    class StubSink:
        def __init__(self, url):
            pass

        def emit_many(self, msgs):
            emitted.extend(msgs)

        def close(self):
            pass

    monkeypatch.setattr(gen_cli_mod, "RedisSink", StubSink)
    res = runner.invoke(gen_app, ["seed", "--sink", "redis", "--resources", "5", "--batch", "3"])
    assert res.exit_code == 0
    assert emitted and len({m.resource_id for m in emitted}) == len(emitted)


# --- cold CLI: real commands against a tmp catalog ----------------------


@pytest.fixture()
def cold_env(tmp_path, monkeypatch):
    from resgraph.cold import store as cold_store
    from resgraph.gen.churn import Churn
    from resgraph.gen.world import World

    monkeypatch.setenv("RESGRAPH_COLD_DIR", str(tmp_path))
    churn = Churn(World(7, 15))
    msgs = list(churn.snapshot()) + [churn.next_message() for _ in range(50)]
    cat = cold_store.get_catalog()
    cold_store.ensure_tables(cat)
    cold_store.append_events(cat, msgs)
    return msgs


def test_cold_init_is_idempotent(tmp_path, monkeypatch):
    from resgraph.cold.cli import app as cold_app

    monkeypatch.setenv("RESGRAPH_COLD_DIR", str(tmp_path))
    for _ in range(2):
        res = runner.invoke(cold_app, ["init"])
        assert res.exit_code == 0 and "cold store ok" in res.stdout


def test_cold_as_of_summary_matches_pure_replay(cold_env):
    from resgraph.cold.cli import app as cold_app

    at = max(m.event_time for m in cold_env).isoformat()
    summary = runner.invoke(cold_app, ["as-of", "--at", at, "--summary"])
    assert summary.exit_code == 0
    s = json.loads(summary.stdout)
    assert s["resources"] == sum(s["by_type"].values())
    replay = runner.invoke(cold_app, ["as-of", "--at", at, "--summary", "--no-snapshots"])
    assert json.loads(replay.stdout) == s  # snapshots are an optimization, never an answer
    rows = runner.invoke(cold_app, ["as-of", "--at", at])
    assert rows.exit_code == 0 and len(json.loads(rows.stdout)) == s["resources"]


def test_cold_history_diff_snapshot_maintain(cold_env):
    from resgraph.cold.cli import app as cold_app

    rid = cold_env[0].resource_id
    hist = runner.invoke(cold_app, ["history", "--id", rid, "--limit", "10"])
    assert hist.exit_code == 0 and json.loads(hist.stdout)
    t0 = min(m.event_time for m in cold_env).isoformat()
    t1 = max(m.event_time for m in cold_env).isoformat()
    diff = runner.invoke(cold_app, ["diff", "--from", t0, "--to", t1])
    assert diff.exit_code == 0 and isinstance(json.loads(diff.stdout), dict)
    assert runner.invoke(cold_app, ["snapshot", "--at", t1]).exit_code == 0
    assert runner.invoke(cold_app, ["maintain"]).exit_code == 0


def test_cold_ingest_body_with_stubbed_stream(tmp_path, monkeypatch):
    import resgraph.cold.cli as cold_cli_mod

    monkeypatch.setenv("RESGRAPH_COLD_DIR", str(tmp_path))

    class StubConsumer:
        def __init__(self, *a, **kw):
            pass

        def run(self, max_messages=None, exit_on_idle=False):
            return {"applied": 0, "skipped": 0}

        def close(self):
            pass

    monkeypatch.setattr(cold_cli_mod, "StreamConsumer", StubConsumer)
    res = runner.invoke(
        cold_cli_mod.app,
        ["ingest", "--max-messages", "1", "--exit-on-idle", "--metrics-port", "9199"],
    )
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {"applied": 0, "skipped": 0}


# --- platform CLI bodies, store boundary stubbed -------------------------


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_schema_init_prints_ok(monkeypatch):
    import resgraph.cli as main_cli

    monkeypatch.setattr(main_cli, "_session", lambda: _FakeSession())
    monkeypatch.setattr(main_cli, "init_schema", lambda s: None)
    res = runner.invoke(main_cli.app, ["schema-init"])
    assert res.exit_code == 0 and "schema ok" in res.stdout


def test_rebuild_parses_at_and_prints_result(monkeypatch, tmp_path):
    import resgraph.cli as main_cli
    import resgraph.cold.rebuild as rebuild_mod
    import resgraph.cold.store as store_mod

    seen = {}

    def fake_rebuild(session, catalog, at):
        seen["at"] = at
        return {"nodes": 3, "edges": 1}

    monkeypatch.setattr(store_mod, "get_catalog", lambda *a, **k: object())
    monkeypatch.setattr(rebuild_mod, "rebuild", fake_rebuild)
    monkeypatch.setattr(main_cli, "_session", lambda: _FakeSession())
    res = runner.invoke(main_cli.app, ["rebuild", "--at", "2026-01-03T00:00:00+00:00"])
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {"nodes": 3, "edges": 1}
    assert seen["at"].isoformat() == "2026-01-03T00:00:00+00:00"


def test_ingest_body_with_stubbed_consumer(monkeypatch):
    import resgraph.cli as main_cli

    class StubConsumer:
        def __init__(self, *a, **kw):
            pass

        def run(self, max_messages=None, exit_on_idle=False):
            return {"read": 5, "applied": 5}

        def close(self):
            pass

    monkeypatch.setattr(main_cli, "Consumer", StubConsumer)
    monkeypatch.setattr(main_cli, "init_schema", lambda s: None)
    monkeypatch.setattr(main_cli, "_session", lambda: _FakeSession())
    res = runner.invoke(main_cli.app, ["ingest", "--max-messages", "5", "--metrics-port", "9198"])
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {"read": 5, "applied": 5}


def test_reconcile_exit_codes_and_oracle_loading(monkeypatch, tmp_path):
    import resgraph.cli as main_cli
    import resgraph.cold.store as store_mod
    import resgraph.reconcile as rec_mod
    from resgraph.gen.churn import Churn
    from resgraph.gen.world import World

    msgs = list(Churn(World(7, 5)).snapshot())
    oracle_path = tmp_path / "oracle.jsonl"
    lines = [
        json.dumps(
            {
                "resource_id": m.resource_id,
                "resource_type": m.resource_type.value,
                "sequence": m.sequence,
                "attrs": dict(m.attrs),
                "relationships": [
                    {"type": r.type, "target_id": r.target_id} for r in m.relationships
                ],
            }
        )
        for m in msgs
    ]
    oracle_path.write_text("\n".join(lines) + "\n\n")  # trailing blank: the skip branch

    state = {}

    def fake_reconcile(session, catalog, oracle):
        state["oracle"] = oracle
        return {"ok": state["verdict"]}

    monkeypatch.setattr(store_mod, "get_catalog", lambda *a, **k: object())
    monkeypatch.setattr(rec_mod, "reconcile", fake_reconcile)
    monkeypatch.setattr(main_cli, "_session", lambda: _FakeSession())

    state["verdict"] = True
    ok = runner.invoke(main_cli.app, ["reconcile", "--oracle", str(oracle_path)])
    assert ok.exit_code == 0
    assert len(state["oracle"]) == len(msgs)  # load_oracle parsed every row

    state["verdict"] = False
    assert runner.invoke(main_cli.app, ["reconcile"]).exit_code == 1  # the drill's contract


def test_serve_hands_off_to_uvicorn(monkeypatch):
    import uvicorn

    import resgraph.cli as main_cli

    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: called.update(host=host, port=port))
    res = runner.invoke(main_cli.app, ["serve", "--host", "127.0.0.1", "--port", "8123"])
    assert res.exit_code == 0
    assert called == {"host": "127.0.0.1", "port": 8123}
