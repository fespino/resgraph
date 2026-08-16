"""The serve entrypoint: builds the app with probes on and hands uvicorn the
serving knobs — without ever binding a socket."""

from typer.testing import CliRunner

from resgraph.gateway import cli


def test_serve_builds_the_app_and_configures_uvicorn(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    models = tmp_path / "models.yaml"
    models.write_text("haiku:\n  provider: anthropic\n  model: claude-haiku-4-5\n")
    result = CliRunner().invoke(cli.app, ["--models-config", str(models), "--port", "9999"])
    assert result.exit_code == 0, result.output
    assert captured["port"] == 9999
    assert captured["host"] == "127.0.0.1"
    assert captured["timeout_keep_alive"] == 75
    assert captured["app"].title == "resgraph-gateway"
