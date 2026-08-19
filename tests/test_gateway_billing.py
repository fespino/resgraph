"""Billing: keys authenticate, the wallet refuses 402 on empty, the
usage surface reads the meter's records, the cost echo is always on."""

import pytest
import yaml
from fastapi.testclient import TestClient

from resgraph.gateway import server
from resgraph.gateway.accounts import UsageLedger, Wallet, load_accounts, mgmt_authorized

SETUPS = {
    "paid": {"provider": "openai", "base_url": "http://x", "model": "claude-haiku-4-5"},
    "free": {"provider": "ollama", "base_url": "http://y", "model": "qwen2.5:1.5b"},
}

ACCOUNTS = {"accounts": {"replay": {"key_env": "TEST_KEY_REPLAY", "granted_usd": 0.01}}}


class _Client:
    def __init__(self, setup):
        self.setup = setup
        self.messages = self

    def create(self, **kwargs):
        return type(
            "R",
            (),
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": type(
                    "U", (), {"input_tokens": 1000, "output_tokens": 1000, "cache_read_tokens": 0}
                )(),
            },
        )()


def _app(tmp_path, accounts: dict | None = ACCOUNTS):
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(SETUPS))
    apath = tmp_path / "accounts.yaml"
    if accounts is not None:
        apath.write_text(yaml.safe_dump(accounts))
    return server.create_app(
        models_path=models,
        client_factory=_Client,
        registry={},
        ignore_probes=True,
        accounts_path=apath,
        wallet=Wallet(tmp_path / "balances.json"),
        usage_ledger=UsageLedger(tmp_path / "usage.jsonl"),
    )


def _gen(client, key=None, **fields):
    headers = {"x-api-key": key} if key else {}
    return client.post(
        "/v1/generate",
        json={"messages": [{"role": "user", "content": "x"}], **fields},
        headers=headers,
    )


def test_accounts_file_never_holds_secrets():
    with pytest.raises(SystemExit, match="inline key"):
        load_accounts(yaml.safe_dump({"accounts": {"a": {"key": "sk-oops"}}}))
    with pytest.raises(SystemExit, match="key_env"):
        load_accounts(yaml.safe_dump({"accounts": {"a": {"granted_usd": 1}}}))


def test_a_key_authenticates_and_a_bad_key_never_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_REPLAY", "sk-test-1")
    client = TestClient(_app(tmp_path))
    ok = _gen(client, key="sk-test-1", model="free")
    assert ok.status_code == 200
    bad = _gen(client, key="sk-wrong", model="free")
    assert bad.status_code == 401  # unknown key is a 401, never anonymous
    lying = _gen(client, key="sk-test-1", model="free", caller="someone-else")
    assert lying.status_code == 400  # a caller field contradicting the key refuses


def test_an_unset_key_env_never_authenticates(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_KEY_REPLAY", raising=False)
    out = _gen(TestClient(_app(tmp_path)), key="", model="free")
    # empty header -> no key presented; a real probe with any value:
    out = _gen(TestClient(_app(tmp_path)), key="anything", model="free")
    assert out.status_code == 401  # absence of a secret is not a wildcard


def test_the_wallet_charges_paid_traffic_and_refuses_402_when_spent(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_REPLAY", "sk-test-1")
    client = TestClient(_app(tmp_path))
    # 1k in + 1k out on haiku pricing = $0.006 per call against a $0.01 grant
    first = _gen(client, key="sk-test-1", model="paid")
    assert first.status_code == 200
    assert first.json()["cost_usd"] == pytest.approx(0.006)
    # a request's cost is unknown at admission (output length is not in
    # the request), so the check is pre-serve and the LAST request may
    # overshoot: the second call spends into overdraft, the third refuses
    second = _gen(client, key="sk-test-1", model="paid")
    assert second.status_code == 200
    third = _gen(client, key="sk-test-1", model="paid")
    assert third.status_code == 402
    assert "payment_required" in third.json()["detail"]
    assert "spent its balance" in third.json()["detail"]  # YOUR balance, not OUR cap
    assert "$0.0120 of $0.0100" in third.json()["detail"]  # the overdraft is visible


def test_free_traffic_never_decrements_the_wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_REPLAY", "sk-test-1")
    client = TestClient(_app(tmp_path))
    for _ in range(3):
        assert _gen(client, key="sk-test-1", model="free").status_code == 200
    assert (tmp_path / "balances.json").exists() is False  # nothing ever charged


def test_the_cost_echo_is_always_on(tmp_path):
    client = TestClient(_app(tmp_path, accounts=None))
    out = _gen(client, model="paid")  # anonymous: no flag, no account needed
    assert out.status_code == 200
    assert out.json()["cost_usd"] == pytest.approx(0.006)
    free = _gen(client, model="free")
    assert free.json()["cost_usd"] == 0.0  # unpriced is unmetered, echoed as such


def test_the_usage_surface_reads_the_meters_own_records(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY_REPLAY", "sk-test-1")
    client = TestClient(_app(tmp_path))
    _gen(client, key="sk-test-1", model="paid")
    _gen(client, model="free")  # anonymous
    rows = client.get("/v1/usage").json()["data"]
    by_account = {r["account"]: r for r in rows}
    assert by_account["replay"]["cost_usd"] == pytest.approx(0.006)
    assert by_account["replay"]["requests"] == 1
    assert by_account["anonymous"]["cost_usd"] == 0.0
    filtered = client.get("/v1/usage", params={"caller": "replay"}).json()["data"]
    assert [r["account"] for r in filtered] == ["replay"]


def test_the_management_key_gates_the_usage_surface(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path))
    assert client.get("/v1/usage").status_code == 200  # unconfigured: open (laptop)
    monkeypatch.setenv("RESGRAPH_GATEWAY_MGMT_KEY", "mgmt-1")
    assert client.get("/v1/usage").status_code == 403
    assert client.get("/v1/usage", headers={"x-mgmt-key": "mgmt-1"}).status_code == 200
    # an inference key is never a management key
    monkeypatch.setenv("TEST_KEY_REPLAY", "sk-test-1")
    assert client.get("/v1/usage", headers={"x-mgmt-key": "sk-test-1"}).status_code == 403
    assert not mgmt_authorized("sk-test-1")


def test_a_cached_hit_is_recorded_at_zero_cost(tmp_path):
    setups = {
        "det": {
            "provider": "ollama",
            "base_url": "http://y",
            "model": "claude-haiku-4-5",
            "temperature": 0,
        }
    }
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(setups))
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    app = server.create_app(
        models_path=models,
        client_factory=_Client,
        registry={},
        ignore_probes=True,
        accounts_path=tmp_path / "none.yaml",
        usage_ledger=ledger,
    )
    client = TestClient(app)
    cold = _gen(client, model="det")
    hit = _gen(client, model="det")
    assert not cold.json()["cached"] and hit.json()["cached"]
    assert hit.json()["cost_usd"] == 0.0  # a cache hit spends no backend tokens
    rows = ledger.aggregate()
    assert rows[0]["requests"] == 2 and rows[0]["cost_usd"] == pytest.approx(0.006)
