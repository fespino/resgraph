"""One-way OTLP export of recorded runs + the round-trip reconciler."""

import json
import sqlite3

import httpx
from typer.testing import CliRunner

from resgraph.langfuse import cli, otlp, reconcile

RUN = {
    "run_id": "run-7",
    "model": "claude-haiku-4-5",
    "git_ref": "abc1234",
    "started_at": "2026-08-19T10:00:00+00:00",
    "finished_at": "2026-08-19T10:01:00+00:00",
    "tokens_in": 900,
    "tokens_out": 100,
}

EVENTS = [
    {
        "seq": 0,
        "kind": "llm_call",
        "payload": {"turn": 1, "input_tokens": 900, "output_tokens": 100},
        "latency_ms": 1500,
        "tokens": 1000,
        "ts": "2026-08-19T10:00:01+00:00",
    },
    {
        "seq": 1,
        "kind": "tool_call",
        "payload": {"tool": "fetch_resource", "ok": True},
        "latency_ms": 20,
        "tokens": None,
        "ts": "2026-08-19T10:00:03+00:00",
    },
    {
        "seq": 2,
        "kind": "cutoff",
        "payload": {"reason": "tool_calls"},
        "latency_ms": None,
        "tokens": None,
        "ts": "2026-08-19T10:00:50+00:00",
    },
]


def _attrs(span):
    out = {}
    for a in span["attributes"]:
        v = a["value"]
        out[a["key"]] = v.get("stringValue", v.get("intValue", v.get("boolValue")))
    return out


def _spans(doc):
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_the_mapping_is_deterministic_and_keeps_the_trails_clock():
    doc = otlp.run_to_otlp(RUN, EVENTS)
    root, first, tool, cut = _spans(doc)
    assert root["traceId"] == otlp.trace_id("run-7") == otlp.trace_id("run-7")
    assert _attrs(root)["langfuse.session.id"] == "run-7"
    assert root["startTimeUnixNano"] == str(otlp.to_nanos(RUN["started_at"]))
    assert first["parentSpanId"] == root["spanId"]
    a = _attrs(first)
    assert a["langfuse.observation.type"] == "generation"
    assert a["langfuse.observation.model.name"] == "claude-haiku-4-5"
    assert json.loads(a["langfuse.observation.usage_details"]) == {"input": 900, "output": 100}
    assert int(first["endTimeUnixNano"]) - int(first["startTimeUnixNano"]) == 1500 * 1_000_000
    assert tool["name"] == "fetch_resource"
    assert _attrs(tool)["langfuse.observation.type"] == "tool"
    assert _attrs(cut)["langfuse.observation.type"] == "event"


def test_usage_falls_back_to_the_honest_total_for_older_rows():
    old = {**EVENTS[0], "payload": {"turn": 1}}
    doc = otlp.run_to_otlp(RUN, [old])
    a = _attrs(_spans(doc)[1])
    assert json.loads(a["langfuse.observation.usage_details"]) == {"total": 1000}


def _db(tmp_path):
    path = tmp_path / "audit.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, alert TEXT, git_ref TEXT, model TEXT,"
        " started_at TEXT, finished_at TEXT, tool_calls INTEGER, tokens_in INTEGER,"
        " tokens_out INTEGER, degraded INTEGER, verdict TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (run_id TEXT, seq INTEGER, kind TEXT, payload TEXT,"
        " latency_ms INTEGER, tokens INTEGER, ts TEXT, row_hash TEXT)"
    )
    conn.execute(
        "INSERT INTO runs (run_id, alert, git_ref, model, started_at, finished_at,"
        " tokens_in, tokens_out) VALUES (?,?,?,?,?,?,?,?)",
        ("run-7", "{}", "abc1234", RUN["model"], RUN["started_at"], RUN["finished_at"], 900, 100),
    )
    for e in EVENTS:
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (
                "run-7",
                e["seq"],
                e["kind"],
                json.dumps(e["payload"]),
                e["latency_ms"],
                e["tokens"],
                e["ts"],
                "h",
            ),
        )
    conn.commit()
    conn.close()
    return path


def test_export_dry_run_prints_the_document(tmp_path):
    r = CliRunner().invoke(cli.app, ["export", "--db", str(_db(tmp_path)), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert otlp.trace_id("run-7") in r.output


def test_export_refuses_without_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    r = CliRunner().invoke(cli.app, ["export", "--db", str(_db(tmp_path))])
    assert r.exit_code == 1
    assert "environment" in str(r.exception)


def test_export_posts_otlp_with_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers, doc=json)
        return httpx.Response(207, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    r = CliRunner().invoke(cli.app, ["export", "--db", str(_db(tmp_path))])
    assert r.exit_code == 0, r.output
    assert seen["url"].endswith(otlp.OTLP_TRACES_PATH)
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert seen["headers"]["x-langfuse-ingestion-version"] == otlp.INGESTION_VERSION
    assert len(_spans(seen["doc"])) == 4


def test_the_refusal_paths_name_their_leg(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).execute("CREATE TABLE runs (run_id TEXT, started_at TEXT)")
    r = CliRunner().invoke(cli.app, ["export", "--db", str(empty), "--dry-run"])
    assert "no runs recorded" in str(r.exception)
    db = str(_db(tmp_path))
    r = CliRunner().invoke(cli.app, ["export", "--db", db, "--run-id", "nope", "--dry-run"])
    assert "'nope' is not in" in str(r.exception)

    def fail(url, **kwargs):
        return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fail)
    r = CliRunner().invoke(cli.app, ["export", "--db", db])
    assert "export refused: HTTP 500" in str(r.exception)
    monkeypatch.setattr(httpx, "get", fail)
    r = CliRunner().invoke(cli.app, ["roundtrip", "--db", db])
    assert "row leg refused: HTTP 500" in str(r.exception)

    def rows_then_fail(url, **kwargs):
        if url.endswith("/observations"):
            return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))
        return fail(url)

    monkeypatch.setattr(httpx, "get", rows_then_fail)
    r = CliRunner().invoke(cli.app, ["roundtrip", "--db", db])
    assert "aggregate leg refused: HTTP 500" in str(r.exception)


def _obs(seq, **over):
    return {"metadata": {"audit_seq": seq}, **over}


def test_reconcile_rows_names_every_mismatch():
    obs = [
        _obs(0, latency_ms=1500, total_tokens=1000),
        _obs(2, latency_ms=0),
        _obs(9),
    ]
    got = reconcile.reconcile_rows(EVENTS, obs)
    assert got == [
        "seq 1 (tool_call): no observation came back",
        "seq 9: observation exists that the trail never wrote",
    ]
    drifted = [_obs(0, latency_ms=1400, total_tokens=999)]
    got = reconcile.reconcile_rows(EVENTS[:1], drifted)
    assert "latency 1400ms back vs 1500ms recorded" in got[0]
    assert "999 tokens back vs 1000 recorded" in got[1]


def test_reconcile_aggregate_compares_one_number_each_side():
    assert (
        reconcile.reconcile_aggregate(RUN, [{"sum_totalTokens": "1000"}], "sum_totalTokens") == []
    )
    got = reconcile.reconcile_aggregate(RUN, [{"sum_totalTokens": 900}], "sum_totalTokens")
    assert got == ["aggregate: 900 tokens from their metrics vs 1000 from the runs table"]


def test_observation_rows_parses_string_metadata():
    rows = reconcile.observation_rows(
        {"data": [{"metadata": '{"audit_seq": 3}'}, {"metadata": "x"}]}
    )
    assert rows[0]["metadata"] == {"audit_seq": 3}
    assert rows[1]["metadata"] == {}


def test_roundtrip_reconciles_both_legs(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    pages = {
        "/api/public/v2/observations": {
            "data": [
                _obs(0, latency_ms=1500, total_tokens=1000),
                _obs(1, latency_ms=20),
                _obs(2),
            ]
        },
        "/api/public/v2/metrics": {"data": [{"sum_totalTokens": 1000}]},
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        body = next(v for k, v in pages.items() if url.endswith(k))
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    db = str(_db(tmp_path))
    r = CliRunner().invoke(cli.app, ["roundtrip", "--db", db])
    assert r.exit_code == 0, r.output
    assert "both legs reconcile" in r.output
    pages["/api/public/v2/metrics"] = {"data": [{"sum_totalTokens": 400}]}
    r2 = CliRunner().invoke(cli.app, ["roundtrip", "--db", db])
    assert r2.exit_code == 1
    assert "MISMATCH aggregate" in r2.output
