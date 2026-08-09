"""Build a store-degraded companion set for a dataset (#152, #158).

Deterministic: the first item of each scenario type in file order,
tagged so the runner kills the named store mid-run. The fault target
is an argument, never a default — INC-002 was a drill whose target
nobody had checked against the workload. The world is unchanged —
same seed, same planted cause — because the graded question changes
instead (SPEC.md, D29a addendum).

Usage:
    uv run python scripts/make_degraded.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/degraded-cold.jsonl \
        cold
"""

import sys
from pathlib import Path

from resgraph.evals.sanitize import sanitize_findings
from resgraph.gen.scenarios import Scenario, rebuild

SUFFIX = {"hot": "-dg", "cold": "-dgc"}


def main(dataset_path: str, out_path: str, fault: str) -> None:
    if fault not in SUFFIX:
        raise SystemExit(f"fault target must be one of {sorted(SUFFIX)}, got {fault!r}")
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
            update={
                "id": f"{spec.id}{SUFFIX[fault]}",
                "tags": [*spec.tags, "store_degraded", f"fault:{fault}"],
            }
        )
        findings = sanitize_findings(item)
        if findings:
            raise SystemExit(
                f"sanitization refused {item.id}:\n" + "\n".join(f"  {f}" for f in findings)
            )
        rebuild(item)
        out.append(item)
        print(f"degraded ({fault}): {spec.id} -> {item.id}")
    Path(out_path).write_text("".join(i.model_dump_json() + "\n" for i in out))
    print(f"{len(out)} items -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
