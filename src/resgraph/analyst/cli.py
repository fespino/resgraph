"""resgraph-analyst — the audit query surface (D27).

Every question here is answered from the audit store alone, with the
agent stopped: the run timeline, what the agent read and wrote
(--touched), the cross-run write history (--tool apply_remediation),
and the span tree (--trace).
"""

from pathlib import Path
from typing import Any

import typer

from .audit import DEFAULT_DB, AuditStore, parse_since

app = typer.Typer(help="resgraph analyst CLI.", add_completion=False)


@app.callback()
def main() -> None:
    """Keeps `audit` a named subcommand as siblings arrive (triage)."""


def _summary(kind: str, p: dict[str, Any]) -> str:
    match kind:
        case "llm_call":
            return f"turn {p['turn']} → {p['tool_uses']} tool use(s)"
        case "tool_call":
            return f"{p['tool']} {'ok' if p['ok'] else 'ERROR'}"
        case "step":
            return f"{p['action']} #{p['step_index']} {p['status']} {p['target']}"
        case "approval":
            verdict = "approved" if p["approved"] else "rejected"
            return (
                f"{verdict} by {p['approver']}"
                f" (plan {p['plan_hash'][:8]}, {len(p['applied'])} applied,"
                f" {len(p['skipped'])} skipped)"
            )
        case "cutoff":
            return f"{p['reason']} exhausted at call {p['calls_used']}"
        case _:
            pass
    return ""


def _cost(e: dict[str, Any]) -> str:
    parts = []
    if e.get("latency_ms") is not None:
        parts.append(f"{e['latency_ms']}ms")
    if e.get("tokens") is not None:
        parts.append(f"{e['tokens']}tok")
    return "  ".join(parts)


@app.command("audit")
def audit_cmd(
    run_id: str | None = typer.Argument(None, help="Run to inspect; omit with --tool."),
    touched: bool = typer.Option(False, "--touched", help="Distinct resources read/written."),
    trace: bool = typer.Option(False, "--trace", help="Span tree with per-span latency."),
    verify: bool = typer.Option(False, "--verify", help="Recompute the tamper-evidence chain."),
    tool: str | None = typer.Option(None, "--tool", help="Cross-run history for one tool."),
    since: str | None = typer.Option(None, "--since", help="Window for --tool: 7d, 24h, 30m."),
    db: str | None = typer.Option(None, "--db", help="Audit store path [data/analyst-audit.db]."),
) -> None:
    """Timeline for a run, or cross-run tool history."""
    store = AuditStore(Path(db) if db else DEFAULT_DB)
    try:
        if tool is not None:
            window = parse_since(since) if since else None
            for row in store.tool_history(tool, since=window):
                typer.echo(
                    f"{row['ts']}  {row['run_id']}  #{row['seq']}"
                    f"  {_summary(row['kind'], row['payload'])}"
                )
            return
        if run_id is None:
            raise typer.BadParameter("give a run_id, or --tool for cross-run history")
        if verify:
            broken = store.verify_chain(run_id)
            if broken is not None:
                typer.echo(f"chain broken at seq {broken} — the trail was altered there")
                raise typer.Exit(1)
            typer.echo(f"chain ok ({len(store.timeline(run_id))} events)")
            return
        if touched:
            t = store.touched(run_id)
            typer.echo("read:    " + (", ".join(t["read"]) or "(none)"))
            typer.echo("written: " + (", ".join(t["written"]) or "(none)"))
            return
        events = store.timeline(run_id)
        run = store.run_row(run_id)
        if run is not None:
            typer.echo(
                f"run {run_id}  model={run['model']}  git={run['git_ref']}"
                f"  degraded={bool(run['degraded'])}"
                f"  {run['started_at']} → {run['finished_at']}"
            )
        if trace:
            _render_tree(events)
            return
        for e in events:
            line = (
                f"{e['seq']:>3}  {e['ts']}  {e['kind']:<9}"
                f"  {_summary(e['kind'], e['payload'])}  {_cost(e)}"
            )
            typer.echo(line.rstrip())
    finally:
        store.close()


def _render_tree(events: list[dict[str, Any]]) -> None:
    """run → llm_call → its tool_calls; steps and the approval hang off
    the run directly — they happen after the loop has concluded."""
    for i, e in enumerate(events):
        if e["kind"] == "tool_call":
            nested_last = i + 1 == len(events) or events[i + 1]["kind"] != "tool_call"
            branch = "│  └─" if nested_last else "│  ├─"
        else:
            branch = "└─" if i + 1 == len(events) else "├─"
        line = f"{branch} {e['kind']} {_summary(e['kind'], e['payload'])}  {_cost(e)}"
        typer.echo(line.rstrip())
