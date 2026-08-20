"""resgraph-gateway — serve the token path."""

from pathlib import Path

import typer
import uvicorn

from resgraph.gateway.budget import FallForwardBudget
from resgraph.gateway.server import MODELS_PATH, POLICY_PATH, create_app

app = typer.Typer(help="Serving gateway: routing, queues, streaming, health.", add_completion=False)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8080,
    models_config: str = str(MODELS_PATH),
    keep_alive: int = 75,
    fallback_cap_usd: float = typer.Option(5.0, envvar="RESGRAPH_GATEWAY_FALLBACK_CAP_USD"),
) -> None:
    """Run the gateway. Probing is declared per setup (`probe_interval_s`
    in models.yaml) — the loop starts only when a setup opts in.
    Keep-alive is set for streams that hold connections through long
    generations. The fall-forward budget caps the money the failure path
    may spend per UTC day before refusing with the reason stated."""
    uvicorn.run(
        create_app(
            models_path=Path(models_config),
            fallback_budget=FallForwardBudget(cap_usd=fallback_cap_usd),
        ),
        host=host,
        port=port,
        timeout_keep_alive=keep_alive,
    )


@app.command(name="market-pull")
def market_pull(
    out_dir: str = "",
    url: str = "",
) -> None:
    """One polite pull of the reference gateway's public catalog into a
    committed snapshot (full schema shape, authored prose redacted).
    Cadence is manual or at most daily — the pre-flight condition, and
    the endpoint is CDN-cached for 300s anyway."""
    from datetime import UTC, datetime

    from resgraph.gateway import market

    rows = market.fetch(url or market.MODELS_URL)
    now = datetime.now(UTC)
    path = market.snapshot(
        rows,
        path=Path(out_dir or market.SNAPSHOT_DIR) / f"openrouter-{now.date().isoformat()}.json",
        url=url or market.MODELS_URL,
        fetched_at=now.isoformat(timespec="seconds"),
    )
    typer.echo(f"{len(rows)} models -> {path}")


@app.command(name="market-baseline")
def market_baseline(
    snapshot: str = "",
    models_config: str = str(MODELS_PATH),
) -> None:
    """Our catalog against the market's: per endpoint, our effective
    price per mtok next to the market listing for the same weights.
    'free' is local unmetered serving; 'unmatched' is a fact, not a
    zero. Defaults to the newest committed snapshot."""
    import yaml

    from resgraph.evals.pricing import PRICES_PER_MTOK
    from resgraph.gateway import market
    from resgraph.gateway.registry import expand

    path = (
        Path(snapshot)
        if snapshot
        else max(market.SNAPSHOT_DIR.glob("openrouter-*.json"), default=None)
    )
    if path is None:
        raise SystemExit("no market snapshot on file: run market-pull first")
    doc = market.load_snapshot(Path(path))
    table, _ = expand(yaml.safe_load(Path(models_config).read_text()) or {})
    typer.echo(f"market: {doc['source']} @ {doc['fetched_at']} ({doc['model_count']} models)")
    for r in market.baseline(table, PRICES_PER_MTOK, doc["data"]):
        ours = f"${r['ours_per_mtok']:.2f}" if r["ours_per_mtok"] else "free"
        if r["market_id"]:
            them = f"${r['market_per_mtok']:.2f} ({r['market_id']})"
            ratio = f" ratio={r['ratio']}" if r["ratio"] is not None else ""
        else:
            them, ratio = "unmatched", ""
        typer.echo(f"{r['endpoint']}: ours={ours} market={them}{ratio}")


@app.command()
def lifecycle(
    models_config: str = str(MODELS_PATH),
    today: str = "",
) -> None:
    """Sunset blast radius over the registry: per lifecycled endpoint,
    its state and who loses what — task classes routed or floored to
    its alias, callers whose policy names it. --today overrides the
    clock for what-if questions ("what breaks on 2026-12-01?")."""
    from datetime import UTC, datetime

    import yaml

    from resgraph.gateway.registry import expand, load_policy, sunset_blast_radius
    from resgraph.gateway.router import DEFAULT_REGISTRY

    table, aliases = expand(yaml.safe_load(Path(models_config).read_text()) or {})
    policy = load_policy(POLICY_PATH.read_text()) if POLICY_PATH.exists() else {}
    day = today or datetime.now(UTC).date().isoformat()
    routes = {str(k): v for k, v in DEFAULT_REGISTRY.items()}
    rows = sunset_blast_radius(table, aliases, routes, policy, day)
    if not rows:
        typer.echo("no endpoint declares a lifecycle")
        raise typer.Exit()
    for r in rows:
        typer.echo(
            f"{r['endpoint']}: {r['state']}"
            + (f" (sunset {r['sunset']})" if r.get("sunset") else "")
            + f" | task_classes={r['task_classes'] or '-'} callers={r['callers'] or '-'}"
        )
