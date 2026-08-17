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


@app.command("classify")
def classify_cmd(
    pilot: bool = typer.Option(False, "--pilot", help="One flagged run only (the paid-run gate)."),
    judge: str = typer.Option(
        "opus", "--judge", help="Pinned classifier setup in evals/models.yaml."
    ),
    out: str = typer.Option("evals/sentinel/l3-verdicts.jsonl", "--out"),
    cap: int = typer.Option(
        50, "--cap", help="Deliberate daily-cap override for this run [default: the D38 cap]."
    ),
    deferred_only: bool = typer.Option(
        False, "--deferred-only", help="Finish only the runs the cap deferred in --out."
    ),
) -> None:
    """Layer 3 over the funnel's admissions: classify each flagged run
    with the pinned judge, honoring the daily call cap (defer, never
    drop). Writes one verdict row per run with the template hash."""
    import json as _json
    from pathlib import Path as _Path

    from resgraph.evals.providers import build_client, load_setup

    from . import classifier, corpus, scan

    report = scan.scan_corpus()
    rows = [*corpus.iter_benign(), *corpus.load_attacks()]
    flagged = [(r, v) for r, v in zip(rows, report.verdicts, strict=True) if v.reaches_l3]
    if pilot:
        flagged = flagged[:1]
    setup = load_setup(judge, _Path("evals/models.yaml"))
    client = build_client(setup)
    call_cap = classifier.CallCap(cap=cap)
    prior = {}
    if deferred_only:
        prior = {v["run_key"]: v for v in map(_json.loads, _Path(out).read_text().splitlines())}
    verdicts = []
    for row, v in flagged:
        evidence = [f"{f.rule}: {f.reason}" for f in v.l1.flags]
        top_z = sorted(v.l2_z.items(), key=lambda kv: -kv[1])[:3]
        evidence.append(
            "vs this worker's benign baseline (z-scores): "
            + ", ".join(f"{n}={z:.1f}" for n, z in top_z)
        )
        key = (row.get("sentinel") or {}).get(
            "id"
        ) or f"{row.get('run_id')}/{row.get('scenario_id')}/t{row.get('trial')}"
        if deferred_only and not prior.get(key, {}).get("deferred", True):
            verdicts.append(prior[key])
            continue
        c = classifier.classify(client, setup["model"], row, evidence, call_cap)
        truth = (row.get("sentinel") or {}).get("attack_type", "benign")
        verdicts.append({**c.__dict__, "truth": truth})
        typer.echo(f"{c.run_key}: {c.tag} (truth={truth}){' DEFERRED' if c.deferred else ''}")
    _Path(out).write_text("".join(_json.dumps(v, sort_keys=True) + "\n" for v in verdicts))
    typer.echo(f"{len(verdicts)} verdicts -> {out} (template {classifier.TEMPLATE_SHA})")
