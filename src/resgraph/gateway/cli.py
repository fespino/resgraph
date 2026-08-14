"""resgraph-gateway — serve the token path."""

from pathlib import Path

import typer
import uvicorn

from resgraph.gateway.server import MODELS_PATH, create_app

app = typer.Typer(help="Serving gateway: routing, queues, streaming, health.", add_completion=False)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8080,
    models_config: str = str(MODELS_PATH),
    probe_interval: float = 15.0,
    keep_alive: int = 75,
) -> None:
    """Run the gateway. Probes start with the server and drive backend
    health; keep-alive is set for streams that hold connections through
    long generations."""
    uvicorn.run(
        create_app(models_path=Path(models_config), probe_interval_s=probe_interval),
        host=host,
        port=port,
        timeout_keep_alive=keep_alive,
    )
