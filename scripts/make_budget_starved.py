"""Build the budget-starved companion set for a dataset (D29 / #139).

A budget-starved item is an existing scenario re-tagged so the runner
starves the harness (STARVED_TOOL_CALLS instead of the normal
ceiling). The world is unchanged — same seed, same planted cause —
because the graded question changes instead: not "did it find the
cause" (it cannot, by construction) but "did it conclude honestly
under starvation". The cutoff dimension grades that; evidence still
polices any claims the starved report makes.

Deterministic: the first item of each scenario type in file order,
id suffixed "-bs". A recipe file like any other dataset —
rebuildable, committable, sanitize-swept.

Usage:
    uv run python scripts/make_budget_starved.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/budget-starved.jsonl
"""

import sys
from pathlib import Path

from resgraph.evals.sanitize import sanitize_findings
from resgraph.gen.scenarios import Scenario, rebuild


def main(dataset_path: str, out_path: str) -> None:
    specs = [
        Scenario.model_validate_json(line)
        for line in Path(dataset_path).read_text().splitlines()
        if line.strip()
    ]
    picked: dict[str, Scenario] = {}
    for spec in specs:
        picked.setdefault(spec.scenario_type.value, spec)
    out = []
    for spec in picked.values():
        item = spec.model_copy(
            update={"id": f"{spec.id}-bs", "tags": [*spec.tags, "budget_starved"]}
        )
        findings = sanitize_findings(item)
        if findings:
            raise SystemExit(
                f"sanitization refused {item.id}:\n" + "\n".join(f"  {f}" for f in findings)
            )
        rebuild(item)
        out.append(item)
        print(f"starved: {spec.id} -> {item.id}")
    Path(out_path).write_text("".join(i.model_dump_json() + "\n" for i in out))
    print(f"{len(out)} items -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
