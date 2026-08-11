"""Build coverage-gap items: worlds whose deciding evidence is
withheld from the readable log (#180).

The item is chosen by id, never by position: gap fairness depends on
the world's shape, and pilot 2 proved it. A snapshot that already
exhibits the degraded chain lets "started broken, never changed"
explain the alert from readable evidence alone, and the correct answer
stops being a deferral — so the item must have a healthy snapshot whose
alert is inexplicable without the withheld flip. The mechanical check
below is necessary, not sufficient: "explains the symptom" cannot be
mechanized here, so the printed reconstructable-vs-actual comparison is
the selection surface, and the pilot is the gate.

Usage:
    uv run python scripts/make_gap.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/gap-pilot.jsonl \
        decoy-s42007
"""

import sys
from datetime import timedelta
from pathlib import Path

from resgraph.evals.sanitize import sanitize_findings
from resgraph.gen.scenarios import Scenario, log_state, rebuild


def main(dataset_path: str, out_path: str, item_id: str) -> None:
    specs = [
        Scenario.model_validate_json(line)
        for line in Path(dataset_path).read_text().splitlines()
        if line.strip()
    ]
    spec = next((s for s in specs if s.id == item_id), None)
    if spec is None or spec.ground_truth is None:
        raise SystemExit(f"{item_id}: not in {dataset_path}, or has no planted cause")
    gen = rebuild(spec)
    truth = spec.ground_truth
    causal = next(m for m in gen.messages if m.sequence == truth.causal_sequence)
    after = sorted(m.event_time for m in gen.messages if m.event_time > causal.event_time)
    cut = after[0] if after else causal.event_time + timedelta(microseconds=1)

    fired = spec.alert.fired_at
    readable = [m for m in gen.messages if m.sequence == 0 or m.event_time >= cut]
    truncated = log_state(readable, fired).get(truth.causal_resource)
    full = log_state(gen.messages, fired).get(truth.causal_resource)
    if truncated is None or full is None or truncated.attrs == full.attrs:
        raise SystemExit(
            f"{item_id}: the cut does not change what the agent can reconstruct "
            f"about {truth.causal_resource} at alert time — the gap would hide nothing"
        )
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
    print(
        f"gap: {spec.id} -> {item.id} (coverage starts {cut.isoformat()}; "
        f"reconstructable {truncated.attrs} vs actual {full.attrs})"
    )


if __name__ == "__main__":
    main(*sys.argv[1:4])
