"""resgraph-gateway — serve the token path."""

from pathlib import Path

import typer
import uvicorn

from resgraph.gateway.budget import FallForwardBudget
from resgraph.gateway.server import MODELS_PATH, create_app

app = typer.Typer(help="Serving gateway: routing, queues, streaming, health.", add_completion=False)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8080,
    models_config: str = str(MODELS_PATH),
    probe_interval: float = 15.0,
    keep_alive: int = 75,
    fallback_cap_usd: float = typer.Option(5.0, envvar="RESGRAPH_GATEWAY_FALLBACK_CAP_USD"),
) -> None:
    """Run the gateway. Probes start with the server and drive backend
    health; keep-alive is set for streams that hold connections through
    long generations. The fall-forward budget caps the money the failure
    path may spend per UTC day before refusing with the reason stated."""
    uvicorn.run(
        create_app(
            models_path=Path(models_config),
            probe_interval_s=probe_interval,
            fallback_budget=FallForwardBudget(cap_usd=fallback_cap_usd),
        ),
        host=host,
        port=port,
        timeout_keep_alive=keep_alive,
    )
