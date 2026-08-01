import pytest


@pytest.fixture(autouse=True)
def _telemetry_dir(tmp_path, monkeypatch):
    """Wide events (D17) go to a per-test directory, never the repo."""
    monkeypatch.setenv("RESGRAPH_TELEMETRY_DIR", str(tmp_path / "telemetry"))
