"""resgraph-evals — run the suite against live stores, report a run."""

import json
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(help="Analyst eval harness (SPEC D24).", add_completion=False)

DEFAULT_MODEL = "claude-opus-4-8"


@app.command()
def run(
    scenarios: str = "evals/scenarios/base.jsonl",
    trials: int = 3,
    model: str = DEFAULT_MODEL,
    judge_model: str = DEFAULT_MODEL,
    no_judge: bool = False,
    out_dir: str = "evals/runs",
    max_tool_calls: int = 15,
    thinking: str = "adaptive",
    resume: str = "",
    max_cost: float = 0.0,
    skip_preflight: bool = False,
    max_item_cost: float = 0.0,
    max_item_seconds: float = 0.0,
    judge_daily_cap: float = 10.0,
    no_skill: bool = False,
    worker: str = "",
    judge: str = "",
    workers_config: str = "evals/workers.yaml",
) -> None:
    """Run every scenario x trials against docker stores + the API;
    one JSONL row per (item, trial) lands in out_dir. --resume PATH
    appends to a truncated run file, skipping completed rows.
    --max-cost USD stops the run (resume-ready) once the estimated
    worker spend reaches the cap; 0 means no cap. --max-item-cost /
    --max-item-seconds are per-run harness ceilings (D29): breach
    injects a final conclude-now turn and the row lands degraded with
    its cutoff_reason. --judge-daily-cap USD trips the judge spend
    breaker (ledger in data/judge-spend.json). --no-skill drops the
    playbook from the prefix (a labeled fingerprint change, the skill
    arm). Run with a project-scoped API key that carries its own spend
    cap — the key is read from the environment and never written
    anywhere. --worker NAME selects a setup from --workers-config (provider,
    model, endpoint, determinism knobs, and extra_args request kwargs; the name
    is recorded per row and is the arm label); without it the worker is --model
    on the Anthropic SDK. A named worker carries its own thinking in extra_args
    (e.g. haiku omits it — the model rejects a thinking param), so --thinking
    governs only the bare --model path. --judge NAME selects the judge setup the
    same way (keep it constant across arms); without it the judge is
    --judge-model on Anthropic. Token price is by model (PRICES_PER_MTOK); a
    model with no listed price is unmetered."""
    from resgraph.graph.client import get_driver

    from .providers import build_client, load_setup
    from .runner import load_scenarios, run_eval

    cfg = Path(workers_config)
    worker_setup: dict[str, Any] = (
        load_setup(worker, cfg) if worker else {"provider": "anthropic", "model": model}
    )
    worker_model: str = worker_setup["model"]
    worker_client = build_client(worker_setup)
    provenance: dict[str, Any] = {"worker": worker_setup}

    # Per-worker request kwargs (thinking, constrained-decoding knobs) ride the
    # setup's extra_args, so `--worker haiku` carries "no thinking" itself
    # rather than needing --thinking at the call site. The bare --model path has
    # no setup, so --thinking supplies the Anthropic default there.
    worker_extra_args: dict[str, Any] = dict(worker_setup.get("extra_args") or {})
    if not worker and thinking == "adaptive" and "thinking" not in worker_extra_args:
        worker_extra_args["thinking"] = {"type": "adaptive"}

    judge_client = None
    judge_model_resolved: str | None = None
    if not no_judge:
        judge_setup: dict[str, Any] = (
            load_setup(judge, cfg) if judge else {"provider": "anthropic", "model": judge_model}
        )
        judge_model_resolved = judge_setup["model"]
        judge_client = build_client(judge_setup)
        provenance["judge"] = judge_setup

    driver = get_driver()
    driver.verify_connectivity()
    out = run_eval(
        load_scenarios(Path(scenarios)),
        worker_client,
        driver,
        model=worker_model,
        judge_model=judge_model_resolved,
        trials=trials,
        out_dir=Path(out_dir),
        max_tool_calls=max_tool_calls,
        extra_args=worker_extra_args,
        resume_path=Path(resume) if resume else None,
        max_cost=max_cost or None,
        skip_preflight=skip_preflight,
        max_item_cost=max_item_cost or None,
        max_item_seconds=max_item_seconds or None,
        judge_daily_cap=judge_daily_cap,
        with_skill=not no_skill,
        judge_client=judge_client,
        provenance=provenance,
    )
    print(out)


@app.command()
def report(
    run_path: str,
    baseline: str = "evals/baseline.json",
) -> None:
    """Aggregate a run file; diff against the committed baseline if present."""
    from .report import aggregate, render

    rows = [json.loads(line) for line in Path(run_path).read_text().splitlines() if line.strip()]
    base = None
    baseline_path = Path(baseline)
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text())
    print(render(aggregate(rows), base))


@app.command()
def verify(
    run_path: str,
    rows: int = 0,
    fingerprint: str = "",
    items: int = 0,
    min_trials: int = 0,
) -> None:
    """Verify a run measured something before it is trusted or compared —
    the docs/drills/ pre-mortem gates as a command, not an ad-hoc snippet:
    one model, one fingerprint, every row used tools, zero fabrications
    (the halt), and the expected shape when --rows / --fingerprint /
    --items / --min-trials are given. Exit 1 if any gate fails."""
    from .verify import render as render_verify
    from .verify import verify_run

    run_rows = [
        json.loads(line) for line in Path(run_path).read_text().splitlines() if line.strip()
    ]
    checks, ok = verify_run(
        run_rows,
        expect_rows=rows or None,
        expect_fingerprint=fingerprint or None,
        expect_items=items or None,
        min_trials=min_trials or None,
    )
    print(render_verify(checks, run_rows))
    if not ok:
        raise typer.Exit(1)


@app.command()
def baseline(
    run_path: str,
    out: str = "evals/baseline.json",
) -> None:
    """Write a run's aggregate as the D29b gate baseline (the
    eval-baseline-refresh flow). The baseline is exactly report.aggregate of
    the run it refreshes from, so `gate` compares future runs against the same
    numbers this run produced. Verify the run first; overwrites --out."""
    from .report import aggregate

    rows = [json.loads(line) for line in Path(run_path).read_text().splitlines() if line.strip()]
    Path(out).write_text(json.dumps(aggregate(rows), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} from {run_path} ({len(rows)} rows)")


def _newest_gateable(
    run_dir: Path, baseline_model: str | None = None
) -> tuple[Path | None, list[str]]:
    """Newest run the gate can actually verdict, and the runs skipped to
    reach it (each with why — companion set, too few trials, or a different
    worker than the baseline). Filenames are timestamps, so reverse-sorted is
    newest-first."""
    from .gate import gate_skip_reason

    skipped: list[str] = []
    for path in sorted(run_dir.glob("*.jsonl"), reverse=True):
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            continue
        reason = gate_skip_reason(rows, baseline_model)
        if reason:
            skipped.append(f"{path.name} ({reason})")
            continue
        return path, skipped
    return None, skipped


@app.command()
def gate(
    run_path: str,
    baseline: str = "evals/baseline.json",
    md: bool = False,
) -> None:
    """Block a merge on an eval regression (D29b). Aggregates the run,
    compares it to the committed baseline, prints the verdict.
    Fabrications block unconditionally; overall pass^k drop >2pp or any
    slice drop >5pp block; a run below k=3, or one measuring a
    different item set than the baseline, is declined. The CI job
    honors the eval-baseline-refresh label; this command does not — it
    only reports what the numbers say.

    RUN_PATH may be a run file or a directory of runs; a directory
    selects the newest GATEABLE run — companion-set runs (their own
    dataset and slice) and runs of a different worker than the baseline
    (a local arm is an `arms` comparison, not a regression) are skipped,
    and the skipped list is named in the output. A base run whose dataset
    drifted is not companion-only, so it is still selected and declines
    loudly.

    Exit codes: 0 passed (or nothing gateable), 1 blocked, 3 declined
    (cannot verdict), 4 evidence unreadable."""
    from .gate import evaluate, render_skip_md, render_verdict, render_verdict_md
    from .report import aggregate

    baseline_path = Path(baseline)
    if not baseline_path.exists():
        raise typer.BadParameter(f"no baseline at {baseline}; nothing to gate against")
    try:
        base = json.loads(baseline_path.read_text())
    except (OSError, ValueError) as exc:
        detail = f"cannot read the baseline: {exc}"
        print(render_skip_md(f"**ERROR** — {detail}") if md else f"EVAL GATE: ERROR — {detail}")
        raise typer.Exit(4) from exc
    target = Path(run_path)
    note = ""
    if target.is_dir():
        selected, skipped = _newest_gateable(target, base.get("model"))
        if selected is None:
            msg = (
                "No gateable run committed — companion sets and sub-k runs are not gate "
                "candidates. A behavior change ships its own k=3 run of the base dataset."
            )
            print(render_skip_md(msg) if md else f"EVAL GATE: {msg}")
            return
        note = (
            f"skipped {len(skipped)} non-gateable run(s): {', '.join(skipped)}\n" if skipped else ""
        )
        note += f"gating `{selected.name}`"
        if not md:
            print(note)
        target = selected
    try:
        rows = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        detail = f"cannot read the evidence: {exc}"
        print(render_skip_md(f"**ERROR** — {detail}") if md else f"EVAL GATE: ERROR — {detail}")
        raise typer.Exit(4) from exc
    verdict = evaluate(aggregate(rows), base)
    print(render_verdict_md(verdict, note=note) if md else render_verdict(verdict))
    if verdict.undecided:
        raise typer.Exit(3)
    if not verdict.passed:
        raise typer.Exit(1)


@app.command()
def arms(
    specs: list[str],
    baseline: str = "opus",
) -> None:
    """Compare labeled arms of the same suite. Each SPEC is
    label=run_file; the table shows pass^k, worker cost, and cost per
    passed triage, with deltas against the --baseline arm.

    Exit code 3 if the arms measure different item sets — a cost
    comparison across different work is declined, not faked."""
    from .arms import arm_summary, compare, render

    summaries = []
    for spec in specs:
        if "=" not in spec:
            raise typer.BadParameter(f"expected label=run_file, got {spec!r}")
        label, path = spec.split("=", 1)
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        summaries.append(arm_summary(label, rows))
    comparison = compare(summaries, baseline)
    print(render(summaries, comparison))
    if not comparison["comparable"]:
        raise typer.Exit(3)


@app.command("skill-value")
def skill_value_cmd(with_run: str, without_run: str) -> None:
    """Ledger the change-forensics playbook's value across the paired
    skill arm: available / retrieved / invoked / relevant. Pass the
    with-skill and without-skill run files.

    Exit code 3 if the two arms measure different item sets."""
    from .skillvalue import render, skill_value

    def _load(path: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]

    value = skill_value(_load(with_run), _load(without_run))
    print(render(value))
    if not value["comparable"]:
        raise typer.Exit(3)
