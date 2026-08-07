"""Build the store-degraded companion set for a dataset (#152).

A store-degraded item is an existing scenario re-tagged so the runner
kills the hot store after DEGRADED_KILL_AFTER tool calls. The world is
unchanged — same seed, same planted cause — because the graded
question changes instead: not "did it find the cause" (the graph
holding it is gone) but "did the report say what it lost". The
degraded dimension grades that; evidence still polices any claim the
report does make, which is how fabrication-after-the-kill is caught.

Deterministic: the first item of each scenario type in file order, id
suffixed "-dg". A recipe file like any other dataset — rebuildable,
committable, sanitize-swept.

Usage:
    uv run python scripts/make_degraded.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/degraded.jsonl
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
            update={"id": f"{spec.id}-dg", "tags": [*spec.tags, "store_degraded"]}
        )
        findings = sanitize_findings(item)
        if findings:
            raise SystemExit(
                f"sanitization refused {item.id}:\n" + "\n".join(f"  {f}" for f in findings)
            )
        rebuild(item)
        out.append(item)
        print(f"degraded: {spec.id} -> {item.id}")
    Path(out_path).write_text("".join(i.model_dump_json() + "\n" for i in out))
    print(f"{len(out)} items -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
