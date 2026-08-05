"""Build the re-skin companion set for a dataset (D24 / #103).

A re-skin holds an item's causal structure fixed — same scenario
type, same depth — and regenerates every surface detail (resource
names, attribute values, distractors) under a shifted seed. If the
agent's scores drop on re-skins, the original scores were partly
template-reading: memorized generator surface, not graph reasoning.

Deterministic: companion seed = original seed + SHIFT, so the
companion set is a recipe file like any other dataset — rebuildable,
committable, and sanitize-swept by the existing CI test.

Usage:
    uv run python scripts/make_reskins.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/reskin-100k.jsonl
"""

import sys
from pathlib import Path

from resgraph.evals.sanitize import sanitize_findings
from resgraph.gen.scenarios import Scenario, rebuild, reskin

SHIFT = 100_000


def main(dataset_path: str, out_path: str) -> None:
    specs = [
        Scenario.model_validate_json(line)
        for line in Path(dataset_path).read_text().splitlines()
        if line.strip()
    ]
    ids = {s.id for s in specs}
    out = []
    for spec in specs:
        companion = reskin(spec, seed=spec.seed + SHIFT)
        item = companion.spec.model_copy(update={"tags": [*companion.spec.tags, "reskin"]})
        if item.id in ids:
            raise SystemExit(f"id collision: {item.id}")
        findings = sanitize_findings(item)
        if findings:
            raise SystemExit(
                f"sanitization refused {item.id}:\n" + "\n".join(f"  {f}" for f in findings)
            )
        rebuild(item)
        out.append(item)
        print(f"reskinned: {spec.id} -> {item.id}")
    Path(out_path).write_text("".join(i.model_dump_json() + "\n" for i in out))
    print(f"{len(out)} items -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
