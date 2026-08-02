"""resgraph-evals — run the suite against live stores, report a run."""

import json
from pathlib import Path

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
) -> None:
    """Run every scenario x trials against docker stores + the API;
    one JSONL row per (item, trial) lands in out_dir."""
    from anthropic import Anthropic

    from resgraph.graph.client import get_driver

    from .runner import load_scenarios, run_eval

    driver = get_driver()
    driver.verify_connectivity()
    out = run_eval(
        load_scenarios(Path(scenarios)),
        Anthropic(),
        driver,
        model=model,
        judge_model=None if no_judge else judge_model,
        trials=trials,
        out_dir=Path(out_dir),
        max_tool_calls=max_tool_calls,
        thinking={"type": "adaptive"} if thinking == "adaptive" else None,
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
