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
