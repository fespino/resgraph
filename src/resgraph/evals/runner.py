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
from resgraph.graph.schema import init_schema, wipe
from resgraph.query.executor import QueryContext

from .graders import DimResult, grade_discipline, grade_evidence, grade_found, grade_honesty
from .judge import judge_narrative

NEIGHBORHOOD_CAP = 12


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


def grade_all(
    spec: Scenario,
    result: RunResult,
    catalog: Any,
    *,
    max_tool_calls: int,
    judge_client: Any = None,
    judge_model: str | None = None,
) -> list[DimResult]:
    is_control = spec.ground_truth is None
    if result.report is None:
        names = ["honesty"] if is_control else ["found_top1", "found_top3", "evidence"]
        dims = [DimResult(n, False, "no valid report produced") for n in names]
        dims.append(grade_discipline(result, max_tool_calls=max_tool_calls))
        return dims
    dims: list[DimResult] = []
    if is_control:
        dims.append(grade_honesty(result.report))
    else:
        truth = spec.ground_truth
        if truth is None:
            raise RuntimeError("causal scenario without ground truth")
        dims.extend(grade_found(result.report, truth))
        edges, log_sequences = evidence_inputs(catalog, spec.alert.fired_at)
        dims.append(grade_evidence(result.report, edges, log_sequences))
    dims.append(grade_discipline(result, max_tool_calls=max_tool_calls))
    if judge_client is not None and judge_model:
        dims.append(
            judge_narrative(
                judge_client,
                model=judge_model,
                narrative=result.report.narrative,
                alert_line=f"{spec.alert.symptom} on {spec.alert.resource_id}",
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
) -> Path:
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

                    toolset = RegistryToolset(qctx)
                    started = time.monotonic()
                    result = run_triage(
                        prompt,
                        toolset,
                        client,
                        model=model,
                        max_tool_calls=max_tool_calls,
                        thinking=thinking,
                    )
                    latency = time.monotonic() - started
                    dims = grade_all(
                        spec,
                        result,
                        catalog,
                        max_tool_calls=max_tool_calls,
                        judge_client=client if judge_model else None,
                        judge_model=judge_model,
                    )
                row = envpin | {
                    "scenario_id": spec.id,
                    "scenario_type": spec.scenario_type.value,
                    "tags": spec.tags,
                    "trial": trial,
                    "dims": {d.dim: {"passed": d.passed, "detail": d.detail} for d in dims},
                    "degraded": result.degraded,
                    "tool_calls": result.tool_calls,
                    "turns": result.turns,
                    "tokens": {
                        "input": result.usage.input_tokens,
                        "output": result.usage.output_tokens,
                        "cache_read": result.usage.cache_read_tokens,
                        "cache_creation": result.usage.cache_creation_tokens,
                        "total": result.usage.spent,
                    },
                    "cache_hit_rate": result.usage.cache_hit_rate,
                    "cache_fingerprint": fingerprint,
                    "latency_s": round(latency, 3),
                    "validation_failures": result.validation_failures,
                    "report": result.report.model_dump() if result.report else None,
                }
                f.write(json.dumps(row) + "\n")
                f.flush()
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
