"""Build coverage-gap items: worlds whose deciding evidence is
withheld from the readable log (#180).

Deterministic: picks the first direct-type item, cuts cold coverage
just after the planted causal event, so the live graph shows the
changed state while no readable event explains it. The correct
terminal is a deferral naming the cold store and the withheld window.

Usage:
    uv run python scripts/make_gap.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/gap-pilot.jsonl
"""

import sys
from datetime import timedelta
from pathlib import Path

from resgraph.evals.sanitize import sanitize_findings
from resgraph.gen.scenarios import Scenario, rebuild


def main(dataset_path: str, out_path: str) -> None:
    specs = [
        Scenario.model_validate_json(line)
        for line in Path(dataset_path).read_text().splitlines()
        if line.strip()
    ]
    spec = next(s for s in specs if s.scenario_type.value == "direct" and s.ground_truth)
    gen = rebuild(spec)
    truth = spec.ground_truth
    assert truth is not None
    causal = next(m for m in gen.messages if m.sequence == truth.causal_sequence)
    # cut at the first event after the cause, in the world's own
    # timescale — the readable log resumes immediately, minus the answer
    after = sorted(m.event_time for m in gen.messages if m.event_time > causal.event_time)
    cut = after[0] if after else causal.event_time + timedelta(microseconds=1)
    item = spec.model_copy(
        update={
            "id": f"{spec.id}-gap",
            "tags": [*spec.tags, "coverage_gap"],
            "provenance": {**spec.provenance, "gap_before": cut.isoformat()},
        }
    )
    findings = sanitize_findings(item)
    if findings:
        raise SystemExit(
            f"sanitization refused {item.id}:\n" + "\n".join(f"  {f}" for f in findings)
        )
    rebuild(item)
    Path(out_path).write_text(item.model_dump_json() + "\n")
    print(f"gap: {spec.id} -> {item.id} (coverage starts {cut.isoformat()})")


if __name__ == "__main__":
    main(*sys.argv[1:3])
