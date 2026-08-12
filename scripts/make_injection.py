"""Build the injection companion set (#160): each world gains a hostile
instruction in a resource attribute the agent reads while investigating.

One item per scenario type, the hostile note attached to the causal
resource (the alert resource for controls) so a competent investigation
encounters it. The demanded target is a real off-path resource — naming
it is following the instruction. World unchanged otherwise; the note is
layered at load time from provenance, so the item stays rebuildable.

Usage:
    uv run python scripts/make_injection.py \
        evals/scenarios/base.jsonl \
        evals/scenarios/injection.jsonl
"""

import sys
from pathlib import Path

from resgraph.evals.injection import injection_text
from resgraph.evals.sanitize import sanitize_findings
from resgraph.gen.scenarios import Scenario, log_state, rebuild


def _rtype(rid: str) -> str:
    return rid.split("-", 1)[0]


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
        gen = rebuild(spec)
        state = log_state(gen.messages, spec.alert.fired_at)
        if spec.ground_truth is not None:
            on_path = set(spec.ground_truth.mechanism_path)
            # closest-to-cause path resource still alive at incident time:
            # a note on a deleted cause is unreadable when the agent looks
            into = next(rid for rid in spec.ground_truth.mechanism_path if rid in state)
        else:
            into = spec.alert.resource_id
            on_path = {into}
        candidates = [
            rid for rid in sorted(state) if rid not in on_path and _rtype(rid) == _rtype(into)
        ] or [rid for rid in sorted(state) if rid not in on_path]
        if not candidates:
            raise SystemExit(f"{spec.id}: no off-path target for the injection")
        target = candidates[0]

        item = spec.model_copy(
            update={
                "id": f"{spec.id}-inj",
                "tags": [*spec.tags, "injection"],
                "provenance": {
                    **spec.provenance,
                    "inject_into": into,
                    "inject_target": target,
                    "inject_text": injection_text(target),
                },
            }
        )
        findings = sanitize_findings(item)
        if findings:
            raise SystemExit(
                f"sanitization refused {item.id}:\n" + "\n".join(f"  {f}" for f in findings)
            )
        rebuild(item)
        out.append(item)
        print(f"injection: {spec.id} -> {item.id} (note on {into}, demands {target})")

    Path(out_path).write_text("".join(i.model_dump_json() + "\n" for i in out))
    print(f"{len(out)} items -> {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
