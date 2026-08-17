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
