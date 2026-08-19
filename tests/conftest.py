import pytest


@pytest.fixture(autouse=True)
def _telemetry_dir(tmp_path, monkeypatch):
    """Wide events (D17) go to a per-test directory, never the repo."""
    monkeypatch.setenv("RESGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))


@pytest.fixture(autouse=True)
def _billing_ledgers_in_tmp(tmp_path, monkeypatch):
    """Billing ledgers go to a per-test directory, never data/."""
    from resgraph.gateway import accounts

    monkeypatch.setattr(accounts, "BALANCES_PATH", tmp_path / "gateway-balances.json")
    monkeypatch.setattr(accounts, "USAGE_PATH", tmp_path / "gateway-usage.jsonl")
