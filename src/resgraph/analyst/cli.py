"""resgraph-analyst — the operator's two surfaces.

`audit` answers from the audit store alone, with the agent stopped: the
run timeline, what the agent read and wrote (--touched), the cross-run
write history (--tool apply_remediation), and the span tree (--trace).

`triage` is the whole journey in one command — alert in, investigation,
report, proposed plan, typed approval, execution, every stage on the
trail. The agent names a cause; it never names a mutation. The
remediation vocabulary is the operator's, supplied with --remediate, so
no model output is ever interpreted as an instruction to write.
"""

import subprocess  # nosec B404 — one literal-arg call, see _git_ref
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from .audit import DEFAULT_DB, AuditStore, parse_since

app = typer.Typer(help="resgraph analyst CLI.", add_completion=False)

DEFAULT_MODEL = "claude-opus-4-8"
NEIGHBORHOOD_CAP = 40
GRANT_TTL_S = 900.0


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


def _git_ref() -> str:
    try:
        out = subprocess.run(  # nosec B603 B607 — literal args, no user input
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _typed(value: str) -> str | int | bool:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def parse_remediation(spec: str, default_target: str) -> tuple[str, str, dict[str, Any]]:
    """`action[@target][:k=v,k=v]` — the operator's vocabulary, parsed
    here so a malformed step fails before anything is rendered."""
    head, _, patch_text = spec.partition(":")
    action, _, target = head.partition("@")
    if not action:
        raise typer.BadParameter(f"no action in {spec!r}; expected action[@target][:k=v]")
    patch: dict[str, Any] = {}
    for pair in (p for p in patch_text.split(",") if p.strip()):
        key, sep, value = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"{pair!r} is not k=v in {spec!r}")
        patch[key.strip()] = _typed(value.strip())
    return action, target or default_target, patch


def live_summary(session: Any, resource_id: str, fired_at: datetime, window_h: int) -> Any:
    from resgraph.graph.client import lit
    from resgraph.graph.ingest import read_node

    from .prompts import WorldSummary

    live = session.run(
        lit(
            "MATCH (n) WHERE coalesce(n.deleted, false) = false "
            "AND coalesce(n.phantom, false) = false RETURN n.id AS id"
        )
    )
    counts = Counter(str(r["id"]).split("-", 1)[0] for r in live)
    state = read_node(session, resource_id)
    deps = [f"{resource_id} {t.lower()} {tid}" for t, tid in (state or {}).get("rels", [])]
    inbound = session.run(
        lit("MATCH (m)-[r]->(n {id: $id}) RETURN m.id AS src, type(r) AS t"), id=resource_id
    )
    rdeps = [f"{r['src']} {str(r['t']).lower()} {resource_id}" for r in inbound]
    return WorldSummary(
        resource_counts=dict(counts),
        neighborhood=tuple((sorted(deps) + sorted(rdeps))[:NEIGHBORHOOD_CAP]),
        window_start=fired_at - timedelta(hours=window_h),
        window_end=fired_at,
    )


def _print_report(report: Any, echo: Callable[[str], None] = typer.echo) -> None:
    if report is None:
        echo("no parseable report — the run is on the trail, the verdict is not")
        return
    echo(f"\n{report.narrative}\n")
    if report.no_confident_candidate:
        echo("no confident candidate — that is an answer, not a failure")
    for i, s in enumerate(report.suspects, 1):
        echo(
            f"  {i}. {s.resource_id}  seq={s.sequence}  {s.confidence}"
            f"  path={' -> '.join(s.mechanism_path)}"
        )


@dataclass(frozen=True)
class TriageIO:
    """The journey's collaborators, each one a real boundary: the hot
    store, the model, the tool surface, the ingest stream, the audit
    store, the operator's terminal. Injected so the sequence of stages
    can be tested without reaching into how the command wires them."""

    session: Any
    client: Any
    toolset: Any
    store: AuditStore
    run: Callable[..., Any]
    emit: Callable[[list[Any]], None] | None = None
    ask: Callable[[str], str] = input
    echo: Callable[[str], None] = typer.echo


def triage_journey(
    *,
    resource_id: str,
    symptom: str,
    fired_at: datetime,
    remediate: list[str],
    approver: str,
    model: str,
    window_h: int,
    run_id: str,
    git_ref: str,
    io: TriageIO,
    dry_run: bool = False,
    grant_ttl_s: float = GRANT_TTL_S,
) -> int:
    """Alert in, report out, and — when the operator asks — plan,
    approval, execution. Returns a process exit code."""
    from resgraph.query.executor import QueryContext
    from resgraph.tools.context import CallerContext

    from .approval import approve_plan, render_plan_text
    from .executor import (
        WRITE_SCOPE,
        ApplyRemediationIn,
        apply_remediation,
        capture_pre_state,
        preview_remediation,
    )
    from .prompts import build_prompt
    from .remediation import StepStatus, render_plan

    prompt = build_prompt(
        resource_id=resource_id,
        symptom=symptom,
        fired_at=fired_at,
        summary=live_summary(io.session, resource_id, fired_at, window_h),
    )
    io.store.begin_run(
        run_id,
        alert={
            "resource_id": resource_id,
            "symptom": symptom,
            "fired_at": fired_at.isoformat(),
        },
        model=model,
        git_ref=git_ref,
    )
    io.echo(f"run {run_id}: investigating {symptom} on {resource_id}")
    result = io.run(prompt, io.toolset, io.client, model=model, on_event=io.store.sink(run_id))
    io.store.finish_run(run_id, result)
    _print_report(result.report, io.echo)
    if not remediate:
        io.echo(f"\nreport only — no remediation proposed. Trail: resgraph-analyst audit {run_id}")
        return 0
    if result.report is None or not result.report.suspects:
        io.echo("\nno suspect to remediate — refusing to propose a plan against nothing")
        return 1

    top = result.report.suspects[0].resource_id
    specs = [parse_remediation(spec, top) for spec in remediate]
    plan = render_plan(specs, capture_pre_state(io.session))
    if dry_run:
        # must precede the emit guard: a preview has no write channel
        io.echo(render_plan_text(plan))
        io.echo("\ndry run — these messages would be emitted, and were not:")
        for msg in preview_remediation(plan, read=capture_pre_state(io.session)):
            io.echo(f"  {msg.model_dump_json()}")
        return 0
    if io.emit is None:
        raise RuntimeError("remediation was requested without a write channel to the stream")
    decision = approve_plan(plan, approver=approver, ask=io.ask, echo=io.echo, ttl_s=grant_ttl_s)
    io.store.record_approval(run_id, decision)
    if not decision.approved:
        io.echo("rejected — nothing was applied")
        return 0

    subplan = [plan[i] for i in decision.applied]
    out = apply_remediation(
        ApplyRemediationIn(
            run_id=run_id, owner=approver, steps=subplan, expires_at=decision.expires_at
        ),
        ctx=CallerContext(
            caller="operator",
            scopes=frozenset({WRITE_SCOPE}),
            query=QueryContext(session=io.session),
            emit=io.emit,
        ),
    )
    io.store.record_step_events(run_id, out.events, subplan)
    for index, status in sorted(out.summary.items()):
        io.echo(f"  {index}: {status}")
    io.echo(f"\ntrail: resgraph-analyst audit {run_id}")
    # the summary is per-step final disposition; the event list also
    # carries the 'started' events, which are not outcomes
    if any(status != StepStatus.SUCCEEDED for status in out.summary.values()):
        return 1
    return 0


@app.command("triage")
def triage_cmd(
    resource_id: str = typer.Argument(..., help="The alerting resource."),
    symptom: str = typer.Option("crash_loop", "--symptom", help="What fired."),
    fired_at: str | None = typer.Option(None, "--fired-at", help="ISO-8601 UTC [now]."),
    remediate: Annotated[
        list[str] | None,
        typer.Option("--remediate", help="Propose a step: action[@target][:k=v]. Repeatable."),
    ] = None,
    approver: str = typer.Option("", "--approver", help="Required to propose remediation."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Render the plan and its messages; runs the paid investigation, writes nothing.",
    ),
    grant_ttl_s: float = typer.Option(
        GRANT_TTL_S, "--grant-ttl", help="Seconds an approval stays valid."
    ),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    window_h: int = typer.Option(24, "--window-hours", help="Event window handed to the agent."),
    db: str | None = typer.Option(None, "--db", help="Audit store path."),
    stream: str = typer.Option("resgraph:updates", "--stream", help="Ingest stream to emit on."),
    redis_url: str = typer.Option("redis://localhost:6379", "--redis-url"),
) -> None:
    """Investigate an alert; with --remediate, propose and gate a plan.

    Without --remediate this is read-only: the agent investigates and
    reports. Remediation is a separate, deliberate act — it needs an
    approver, it renders every step with the state a rollback would
    restore, and it takes a typed count at the gate.
    """
    from anthropic import Anthropic

    from resgraph.graph.client import get_driver

    from .harness import run_triage
    from .tools import default_toolset

    steps_requested = list(remediate or [])
    if dry_run and not steps_requested:
        raise typer.BadParameter("--dry-run needs --remediate: there is nothing to preview")
    if steps_requested and not approver and not dry_run:
        raise typer.BadParameter("--remediate needs --approver: execution is attributable")
    fired = datetime.fromisoformat(fired_at) if fired_at else datetime.now(UTC)
    if fired.tzinfo is None:
        raise typer.BadParameter("--fired-at needs an offset; ambiguous time is not accepted (D2)")

    driver = get_driver()
    driver.verify_connectivity()
    session = driver.session()
    store = AuditStore(Path(db) if db else DEFAULT_DB)
    sink = None
    if steps_requested and not dry_run:
        from resgraph.gen.sinks import RedisSink

        sink = RedisSink(redis_url, stream=stream)
    try:
        code = triage_journey(
            resource_id=resource_id,
            symptom=symptom,
            fired_at=fired,
            remediate=steps_requested,
            approver=approver,
            model=model,
            window_h=window_h,
            dry_run=dry_run,
            grant_ttl_s=grant_ttl_s,
            run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            git_ref=_git_ref(),
            io=TriageIO(
                session=session,
                client=Anthropic(),
                toolset=default_toolset(),
                store=store,
                run=run_triage,
                emit=sink.emit_many if sink is not None else None,
            ),
        )
    finally:
        if sink is not None:
            sink.close()
        session.close()
        store.close()
    if code:
        raise typer.Exit(code)


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
