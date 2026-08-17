"""resgraph-sentinel — the misuse-detection surface over the audit trail.

`corpus build` regenerates the seeded attacks from the benign manifest;
`corpus stats` prints the confusion-stream shape both halves must cover.
"""

import json
from collections import Counter

import typer

from . import corpus

app = typer.Typer(no_args_is_help=True, add_completion=False)
corpus_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(corpus_app, name="corpus")


@corpus_app.command("build")
def corpus_build(
    refresh_manifest: bool = typer.Option(
        False, "--refresh-manifest", help="Re-scan evals/runs for clean benign files first."
    ),
) -> None:
    """Rebuild evals/sentinel/attacks.jsonl from the benign manifest.
    Deterministic: same manifest in, byte-identical attacks out."""
    if refresh_manifest:
        runs = corpus.select_benign_runs()
        corpus.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        corpus.MANIFEST_PATH.write_text(json.dumps({"runs": runs}, indent=2) + "\n")
        typer.echo(f"manifest: {len(runs)} benign run files")
    benign = corpus.iter_benign()
    attacks = corpus.build_attacks(benign)
    corpus.write_attacks(attacks)
    typer.echo(f"attacks: {len(attacks)} rows -> {corpus.ATTACKS_PATH}")


@corpus_app.command("stats")
def corpus_stats() -> None:
    """The combined stream both layers are measured on: benign volume and
    seeded attacks by type (the false-positive denominator + the recall
    numerator)."""
    benign = corpus.iter_benign()
    attacks = corpus.load_attacks()
    by_type = Counter(a["sentinel"]["attack_type"] for a in attacks)
    typer.echo(f"benign rows (false-positive denominator): {len(benign)}")
    typer.echo(f"attack rows (recall numerator): {len(attacks)}")
    for t in corpus.ATTACK_TYPES:
        typer.echo(f"  {t}: {by_type[t]}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()


@app.command("scan")
def scan_cmd() -> None:
    """Layers 1+2 over the combined corpus: benign false-positive rate
    first (the headline), then per-layer confusion, per-rule hits, the
    per-type recall, and the funnel into layer 3."""
    from . import scan

    report = scan.scan_corpus()
    combined = report.confusion("l3")
    benign_total = combined["fp"] + combined["tn"]
    typer.echo(f"benign false-positive rate: {combined['fp']}/{benign_total}")
    for layer in ("l1", "l2"):
        c = report.confusion(layer)
        typer.echo(f"{layer}: tp={c['tp']} fp={c['fp']} fn={c['fn']} tn={c['tn']}")
    typer.echo(
        "per-rule (tp/fp): "
        + ", ".join(f"{name}={v['tp']}/{v['fp']}" for name, v in report.per_rule().items())
    )
    typer.echo(
        "recall by type: "
        + ", ".join(
            f"{t}={caught}/{total}"
            for t, (caught, total) in sorted(report.recall_by_type().items())
        )
    )
    reach = sum(1 for v in report.verdicts if v.reaches_l3)
    typer.echo(f"funnel: {reach}/{len(report.verdicts)} runs would reach layer 3")
