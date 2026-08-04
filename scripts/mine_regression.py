"""Mine failure-derived regression items from a run file (D24: every
iteration failure becomes a permanent dataset item).

Selection mirrors the iteration-7 residual decode in EVALS.md — the
two named buckets, nothing else:

- early_conclusion: a transitive item whose found_top3 failed while
  the report hedged (no_confident_candidate true) — an honest miss
  that stopped exploring too early.
- decoy_seduction: a decoy item whose found_top3 failed while the
  report committed (no_confident_candidate false) — a confident
  wrong answer on the planted confounder.

Failed rows outside those buckets are printed, not silently dropped.

Usage:
    uv run python scripts/mine_regression.py \
        evals/runs/20260803T200610Z.jsonl \
        evals/scenarios/base.jsonl \
        evals/scenarios/regression.jsonl

Applies the sanitization checklist (evals/scenarios/SANITIZATION.md)
where a script can: provenance carries references only, every mined
item must rebuild from its recipe, and ids must collide with nothing
in the source dataset.
"""

import json
import sys
from pathlib import Path
from typing import Any

from resgraph.gen.scenarios import Scenario, derive_regression, rebuild


def bucket_for(row: dict[str, Any]) -> str | None:
    if row["dims"].get("found_top3", {}).get("passed", True):
        return None
    committed = (row.get("report") or {}).get("no_confident_candidate") is False
    if row["scenario_type"] == "transitive" and not committed:
        return "early_conclusion"
    if row["scenario_type"] == "decoy" and committed:
        return "decoy_seduction"
    return None


def main(run_path: str, dataset_path: str, out_path: str) -> None:
    rows = [json.loads(ln) for ln in Path(run_path).read_text().splitlines() if ln.strip()]
    specs: dict[str, Scenario] = {}
    for line in Path(dataset_path).read_text().splitlines():
        if line.strip():
            spec = Scenario.model_validate_json(line)
            specs[spec.id] = spec

    run_id = rows[0]["run_id"]
    derived: dict[str, Scenario] = {}
    for row in rows:
        if row["scenario_id"] in derived:
            continue
        bucket = bucket_for(row)
        if bucket is None:
            if not row["dims"].get("found_top3", {}).get("passed", True):
                print(f"skipped (no bucket): {row['scenario_id']}")
            continue
        item = derive_regression(specs[row["scenario_id"]], run_id=run_id, bucket=bucket)
        if item.id in specs:
            raise SystemExit(f"id collision with dataset: {item.id}")
        rebuild(item)
        derived[row["scenario_id"]] = item
        print(f"mined: {item.id} bucket={bucket}")

    out = Path(out_path)
    items = sorted(derived.values(), key=lambda s: s.id)
    out.write_text("".join(i.model_dump_json() + "\n" for i in items))
    print(f"{len(items)} items -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
