"""The eval loop: rebuild the world from seed, load both stores, fire
the harness, grade every dimension, append one JSONL row per
(item, trial).

Store isolation per scenario is absolute — scenario worlds share
resource ids by construction, so the hot store is wiped and reloaded
and the cold store gets a throwaway catalog for every item. Nothing
leaks between scenarios; determinism comes from the generator, not
from cleanup discipline.
"""

import json
import os
import platform
import subprocess  # nosec B404 — fixed-arg git calls below
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from resgraph.analyst.harness import (
    MAX_TOOL_CALLS,
    Prompt,
    RunResult,
    cache_fingerprint,
    run_triage,
)
from resgraph.analyst.prompts import WorldSummary, build_prompt
from resgraph.analyst.tools import RegistryToolset
from resgraph.cold import queries as cold_queries
from resgraph.cold import store as cold_store
from resgraph.gen.scenarios import GeneratedScenario, Scenario, log_state, rebuild
from resgraph.graph.ingest import apply_batch
from resgraph.graph.loader import load_snapshot
from resgraph.graph.schema import init_schema, node_count, wipe
from resgraph.query.executor import QueryContext

from .breaker import JudgeSpendBreaker
from .faults import cold_store_dies_after, hot_store_dies_after
from .graders import (
    DeferralWorld,
    DimResult,
    grade_cutoff,
    grade_deferral_claim,
    grade_degraded,
    grade_discipline,
    grade_evidence,
    grade_found,
    grade_honesty,
)
from .judge import judge_narrative
from .sanitize import secrets

NEIGHBORHOOD_CAP = 12
PREFLIGHT_NODE_CAP = 5_000
# Budget-starved items (tag "budget_starved") run with this tool-call
# ceiling: enough to look, nowhere near enough to finish — the graded
# question is whether the run concludes honestly anyway (D29).
STARVED_TOOL_CALLS = 3
DEGRADED_KILL_AFTER = 2
JUDGE_DAILY_CAP_USD = 10.0

# USD per 1M tokens (input, output), checked 2026-08-05; cache read
# bills at 0.1x input, cache write at 1.25x input.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def load_scenarios(path: Path) -> list[Scenario]:
    return [
        Scenario.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()
    ]


def world_summary(gen: GeneratedScenario) -> WorldSummary:
    state = log_state(gen.messages, gen.spec.alert.fired_at)
    counts: dict[str, int] = {}
    for m in state.values():
        counts[m.resource_type.value] = counts.get(m.resource_type.value, 0) + 1
    target = gen.spec.alert.resource_id
    target_msg = state.get(target)
    deps = [
        f"{target} {r.type} {r.target_id}" for r in (target_msg.relationships if target_msg else [])
    ]
    rdeps = [
        f"{m.resource_id} {r.type} {target}"
        for m in state.values()
        for r in m.relationships
        if r.target_id == target
    ]
    return WorldSummary(
        resource_counts=counts,
        neighborhood=tuple((sorted(deps) + sorted(rdeps))[:NEIGHBORHOOD_CAP]),
        window_start=gen.messages[0].event_time,
        window_end=gen.spec.alert.fired_at,
    )


def load_stores(driver: Any, gen: GeneratedScenario, cold_dir: Path) -> Any:
    snapshot = [m for m in gen.messages if m.sequence == 0]
    churned = [m for m in gen.messages if m.sequence > 0]
    with driver.session() as session:
        wipe(session)
        init_schema(session)
        load_snapshot(session, snapshot)
        apply_batch(session, churned)
    catalog = cold_store.get_catalog(cold_dir)
    cold_store.ensure_tables(catalog)
    cold_store.append_events(catalog, gen.messages)
    return catalog


def evidence_inputs(catalog: Any, at: datetime) -> tuple[set[tuple[str, str]], set[int]]:
    rows = cold_queries.state_at(catalog, at)
    edges = {(row["resource_id"], rel["target_id"]) for row in rows for rel in row["relationships"]}
    scan = catalog.load_table(cold_store.EVENTS).scan(selected_fields=("sequence",)).to_arrow()
    return edges, set(scan.column("sequence").to_pylist())


def _deferral_claim(result: RunResult, catalog: Any) -> DimResult | None:
    if result.report is None or result.report.deferral is None:
        return None
    d = result.report.deferral
    scan = catalog.load_table(cold_store.EVENTS).scan(selected_fields=("event_time",)).to_arrow()
    covered = any(d.window_start <= t < d.window_end for t in scan.column("event_time").to_pylist())
    world = DeferralWorld(
        any_call_failed=any(not c.ok for c in result.trace),
        events_in_window=covered,
    )
    return grade_deferral_claim(d, world)


def fault_for(spec: Scenario):
    """Which store the induced fault targets — named per item, refused
    when absent. INC-002 was a drill aimed at a store the workload
    barely used; an implicit target is how that recurs (#158)."""
    if "fault:cold" in spec.tags:
        return cold_store_dies_after
    if "fault:hot" in spec.tags:
        return hot_store_dies_after
    raise SystemExit(
        f"{spec.id}: store_degraded without a fault target tag (fault:hot or fault:cold)"
    )


def grade_all(
    spec: Scenario,
    result: RunResult,
    catalog: Any,
    *,
    max_tool_calls: int,
    judge_client: Any = None,
    judge_model: str | None = None,
    judge_breaker: JudgeSpendBreaker | None = None,
) -> list[DimResult]:
    is_control = spec.ground_truth is None
    starved = "budget_starved" in spec.tags
    if "store_degraded" in spec.tags:
        dims = [grade_degraded(result)]
        if result.report is not None:
            if is_control:
                dims.append(grade_honesty(result.report))
            else:
                truth = spec.ground_truth
                if truth is None:
                    raise RuntimeError("causal scenario without ground truth")
                # measured, not decisive (D29a addendum)
                dims.extend(grade_found(result.report, truth))
                edges, log_sequences = evidence_inputs(catalog, spec.alert.fired_at)
                dims.append(grade_evidence(result.report, edges, log_sequences))
            claim = _deferral_claim(result, catalog)
            if claim is not None:
                dims.append(claim)
        dims.append(grade_discipline(result, max_tool_calls=max_tool_calls))
        return dims
    if starved:
        # The graded question changes: not "did it find the cause" (it
        # cannot, by construction) but "did it conclude honestly under
        # starvation". Claims it does make stay held to the evidence.
        dims = [grade_cutoff(result)]
        if result.report is not None:
            if is_control:
                dims.append(grade_honesty(result.report))
            else:
                edges, log_sequences = evidence_inputs(catalog, spec.alert.fired_at)
                dims.append(grade_evidence(result.report, edges, log_sequences))
            claim = _deferral_claim(result, catalog)
            if claim is not None:
                dims.append(claim)
        dims.append(grade_discipline(result, max_tool_calls=max_tool_calls))
        return dims
    if result.report is None:
        names = ["honesty"] if is_control else ["found_top1", "found_top3", "evidence"]
        dims = [DimResult(n, False, "no valid report produced") for n in names]
        dims.append(grade_discipline(result, max_tool_calls=max_tool_calls))
        return dims
    dims = []
    if is_control:
        dims.append(grade_honesty(result.report))
    else:
        truth = spec.ground_truth
        if truth is None:
            raise RuntimeError("causal scenario without ground truth")
        dims.extend(grade_found(result.report, truth))
        edges, log_sequences = evidence_inputs(catalog, spec.alert.fired_at)
        dims.append(grade_evidence(result.report, edges, log_sequences))
    claim = _deferral_claim(result, catalog)
    if claim is not None:
        dims.append(claim)
    dims.append(grade_discipline(result, max_tool_calls=max_tool_calls))
    if judge_client is not None and judge_model:
        dims.append(
            judge_narrative(
                judge_client,
                model=judge_model,
                narrative=result.report.narrative,
                alert_line=f"{spec.alert.symptom} on {spec.alert.resource_id}",
                breaker=judge_breaker,
            )
        )
    return dims


def _git_ref() -> str:
    try:
        out = subprocess.run(  # nosec B603 B607 — literal args, no user input
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _store_images() -> dict[str, str]:
    """Image refs from compose.yaml, digest included — the file pins by
    digest, so what it declares is what a started container runs."""
    try:
        root = subprocess.run(  # nosec B603 B607 — literal args, no user input
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
        services = yaml.safe_load((Path(root) / "compose.yaml").read_text())["services"]
        return {name: svc["image"] for name, svc in services.items() if "image" in svc}
    except Exception:
        return {}


def _host() -> dict[str, Any]:
    return {
        "class": "ci" if os.environ.get("CI") else "laptop",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
    }


def preflight_store(driver: Any, *, cap: int = PREFLIGHT_NODE_CAP) -> None:
    """Refuse a hot store that is clearly not an eval scratch store
    (#94: the first paid run wiped a memgraph holding 32 hours of
    data, and the delete OOM'd the store). A scenario world is ~60
    resources plus churn; anything near the cap is someone's data."""
    with driver.session() as session:
        count = node_count(session)
    if count > cap:
        raise SystemExit(
            f"preflight refused: hot store holds {count} nodes (cap {cap}) — "
            "that looks like real data, not an eval scratch store. Wipe it "
            "yourself or point the runner elsewhere; --skip-preflight overrides."
        )


def estimate_cost(tokens: dict[str, int], model: str) -> float:
    """Worker-side estimate from a row's token block. The judge's own
    calls are not in it, so this undercounts slightly: the guard is a
    brake, not a bill — the ledger still comes from the console."""
    if model not in PRICES_PER_MTOK:
        raise SystemExit(f"--max-cost has no pricing for {model}; extend PRICES_PER_MTOK")
    input_rate, output_rate = PRICES_PER_MTOK[model]
    return (
        tokens["input"] * input_rate
        + tokens["output"] * output_rate
        + tokens["cache_read"] * input_rate * 0.1
        + tokens["cache_creation"] * input_rate * 1.25
    ) / 1_000_000


def _usage_tokens(usage: Any) -> dict[str, int]:
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_creation": usage.cache_creation_tokens,
    }


def _record_run_metrics(result: RunResult, latency_s: float, model: str) -> None:
    """Emit the analyst SLO metrics (D29b); no-ops without init_metrics."""
    from resgraph import obs

    obs.ANALYST_RUN_SECONDS.record(latency_s)
    if model in PRICES_PER_MTOK:
        obs.ANALYST_RUN_COST.record(estimate_cost(_usage_tokens(result.usage), model))
    obs.ANALYST_RUNS.add(
        1,
        {"degraded": str(result.degraded).lower(), "cutoff": result.cutoff_reason or "none"},
    )


def assert_row_clean(row_json: str) -> None:
    """No run row is written with secret-shaped content anywhere in it
    (a model can echo its environment). Reuses the dataset secret
    validator; details carry spans, never the match."""
    details = secrets.scan(row_json)
    if details:
        raise SystemExit(
            "refusing to write run row with secret-shaped content: " + "; ".join(details)
        )


def resume_state(
    path: Path, model: str, judge_model: str | None
) -> tuple[str, set[tuple[str, int]], set[str]]:
    """run_id, completed (scenario_id, trial) pairs, and fingerprints of
    an existing run file. Refuses a worker or judge mismatch outright;
    the fingerprint check happens at first execution, where the current
    one is first known."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        raise SystemExit(f"resume refused: {path} has no rows")
    first = rows[0]
    if first["model"] != model or first.get("judge_model") != judge_model:
        raise SystemExit(
            f"resume refused: run file pins model={first['model']} "
            f"judge={first.get('judge_model')}, requested {model}/{judge_model}"
        )
    done = {(r["scenario_id"], r["trial"]) for r in rows}
    prints = {r["cache_fingerprint"] for r in rows if r.get("cache_fingerprint")}
    return first["run_id"], done, prints


def run_eval(
    scenarios: list[Scenario],
    client: Any,
    driver: Any,
    *,
    model: str,
    judge_model: str | None = None,
    trials: int = 1,
    out_dir: Path = Path("evals/runs"),
    max_tool_calls: int = MAX_TOOL_CALLS,
    thinking: dict[str, Any] | None = None,
    resume_path: Path | None = None,
    max_cost: float | None = None,
    skip_preflight: bool = False,
    max_item_cost: float | None = None,
    max_item_seconds: float | None = None,
    judge_daily_cap: float = JUDGE_DAILY_CAP_USD,
) -> Path:
    if max_cost is not None or max_item_cost is not None:
        estimate_cost({"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}, model)
    judge_breaker = (
        JudgeSpendBreaker(
            cap_usd=judge_daily_cap, model=judge_model, prices_per_mtok=PRICES_PER_MTOK
        )
        if judge_model
        else None
    )
    if not skip_preflight:
        preflight_store(driver)
    spent_usd = 0.0
    done: set[tuple[str, int]] = set()
    prior_fingerprints: set[str] = set()
    if resume_path is not None:
        run_id, done, prior_fingerprints = resume_state(resume_path, model, judge_model)
        out = resume_path
    else:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{run_id}.jsonl"
    envpin = {
        "run_id": run_id,
        "git_ref": _git_ref(),
        "model": model,
        "judge_model": judge_model,
        "thinking": thinking,
        "stores": _store_images(),
        "host": _host(),
    }
    with out.open("a" if resume_path is not None else "w") as f:
        for spec in scenarios:
            gen = rebuild(spec)
            summary = world_summary(gen)
            prompt = build_prompt(
                resource_id=spec.alert.resource_id,
                symptom=spec.alert.symptom,
                fired_at=spec.alert.fired_at,
                summary=summary,
            )
            for trial in range(trials):
                if (spec.id, trial) in done:
                    continue
                fingerprint = cache_fingerprint(prompt, RegistryToolset(QueryContext))
                if prior_fingerprints and fingerprint not in prior_fingerprints:
                    raise SystemExit(
                        "resume refused: prompt/tool fingerprint differs from the run file"
                    )
                with tempfile.TemporaryDirectory() as tmp:
                    catalog = load_stores(driver, gen, Path(tmp))

                    def qctx(catalog: Any = catalog) -> QueryContext:
                        return QueryContext(
                            session_factory=driver.session, catalog_factory=lambda: catalog
                        )

                    toolset = RegistryToolset(
                        fault_for(spec)(
                            DEGRADED_KILL_AFTER,
                            session_factory=driver.session,
                            catalog_factory=lambda c=catalog: c,
                        )
                        if "store_degraded" in spec.tags
                        else qctx
                    )
                    item_max_calls = (
                        STARVED_TOOL_CALLS if "budget_starved" in spec.tags else max_tool_calls
                    )
                    started = time.monotonic()
                    result = run_triage(
                        prompt,
                        toolset,
                        client,
                        model=model,
                        max_tool_calls=item_max_calls,
                        thinking=thinking,
                        max_cost_usd=max_item_cost,
                        cost_fn=(
                            (lambda u: estimate_cost(_usage_tokens(u), model))
                            if max_item_cost is not None
                            else None
                        ),
                        max_wall_s=max_item_seconds,
                    )
                    latency = time.monotonic() - started
                    dims = grade_all(
                        spec,
                        result,
                        catalog,
                        max_tool_calls=item_max_calls,
                        judge_client=client if judge_model else None,
                        judge_model=judge_model,
                        judge_breaker=judge_breaker,
                    )
                tokens = {
                    "input": result.usage.input_tokens,
                    "output": result.usage.output_tokens,
                    "cache_read": result.usage.cache_read_tokens,
                    "cache_creation": result.usage.cache_creation_tokens,
                    "total": result.usage.spent,
                }
                _record_run_metrics(result, latency, model)
                row = envpin | {
                    "scenario_id": spec.id,
                    "scenario_type": spec.scenario_type.value,
                    "source": spec.provenance.get("source", "planted"),
                    "tags": spec.tags,
                    "trial": trial,
                    "dims": {d.dim: {"passed": d.passed, "detail": d.detail} for d in dims},
                    "degraded": result.degraded,
                    "deferred": bool(result.report and result.report.deferral),
                    "cutoff_reason": result.cutoff_reason,
                    "tool_calls": result.tool_calls,
                    "tool_trace": [{"tool": c.name, "ok": c.ok} for c in result.trace],
                    "turns": result.turns,
                    "tokens": tokens,
                    "cache_hit_rate": result.usage.cache_hit_rate,
                    "cache_fingerprint": fingerprint,
                    "latency_s": round(latency, 3),
                    "validation_failures": result.validation_failures,
                    "report": result.report.model_dump() if result.report else None,
                }
                row_json = json.dumps(row)
                assert_row_clean(row_json)
                f.write(row_json + "\n")
                f.flush()
                if max_cost is not None:
                    spent_usd += estimate_cost(tokens, model)
                    if spent_usd >= max_cost:
                        print(
                            f"stopping: estimated ${spent_usd:.2f} reached "
                            f"--max-cost {max_cost:.2f}; completed rows are banked. "
                            f"Resume with: resgraph-evals run --trials {trials} "
                            f"--resume {out}"
                        )
                        return out
    return out


def build_prompt_for(spec: Scenario) -> Prompt:
    """The exact prompt a scenario produces — exposed for trace tooling."""
    gen = rebuild(spec)
    return build_prompt(
        resource_id=spec.alert.resource_id,
        symptom=spec.alert.symptom,
        fired_at=spec.alert.fired_at,
        summary=world_summary(gen),
    )
