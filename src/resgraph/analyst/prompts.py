"""Prompt content for the analyst — words live here, message order in
the harness.

Every section carries a PREFIX/SUFFIX verdict in docs/prompt-audit.md;
this module and that table change together. The prefix is one system
block ending in a cache breakpoint; the world summary rides a second,
uncached system block; the alert is the first user message.

The triage-discipline section is the committed change-forensics skill
body — the playbook was written once, and the agent's prefix is its
second consumer after MCP. Loading it through the skill loader keeps
its validation: a drifted skill file fails at prompt build, not
silently at run time.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from resgraph.mcp.skills import load_skill

from .harness import Prompt
from .models import TriageReport

SKILL_PATH = Path(__file__).resolve().parents[3] / "skills" / "change-forensics" / "SKILL.md"

# Section labels the audit table must carry a verdict for; a test holds
# the two artifacts together.
AUDITED_SECTIONS = (
    "identity",
    "triage discipline",
    "tool guidance",
    "output contract",
    "worked example",
    "world summary",
    "alert payload",
)

_WORKED_EXAMPLE = """\
# Worked examples — the same investigation, two different verdicts

## Worked example 1 — a quiet window, concluded correctly

Alert: latency_high on lb-000104, fired at 2026-01-01T00:00:00.0031Z.
The tight-window diff shows five changed resources. The most tempting
correlate: vm-000121 runs two of the lb's targets and changed
state=starting -> running forty seconds before the alert.
Investigation: blast_radius(lb-000104) contains vm-000121;
resource_history(vm-000121) shows sequence 87 is that state change —
a recovery, not a fault — and nothing else in the radius moved.

The correct report:

{
  "suspects": [
    {
      "sequence": 87,
      "resource_id": "vm-000121",
      "mechanism_path": ["vm-000121", "lb-000104"],
      "confidence": "low",
      "evidence": [
        "only radius-intersecting change in the window; a recovery cannot explain rising latency"
      ]
    }
  ],
  "no_confident_candidate": true,
  "degraded": false,
  "narrative": "Quiet window: the only in-radius change is a recovery; no confident candidate."
}

The shape to copy: the tempting correlate IS listed — at low
confidence, with an evidence line saying why it falls short — and the
flag is true because nothing found actually explains the symptom.

## Worked example 2 — a deep cause, found and committed to

Alert: crash_loop on container-000217, fired at 2026-01-01T00:00:00.0042Z.
The tight-window diff shows six changed resources; none of them is
the container itself, so the window LOOKS quiet at depth 1.
Investigation: blast_radius(container-000217, depth=2) reaches
vm-000132 (runs_on) and sg-000108 (the vm's security group).
world_diff intersected with that radius leaves one mover: sg-000108.
resource_history(sg-000108) shows sequence 91: open_to_world
false -> true, ninety seconds before the alert.

The correct report:

{
  "suspects": [
    {
      "sequence": 91,
      "resource_id": "sg-000108",
      "mechanism_path": ["sg-000108", "vm-000132", "container-000217"],
      "confidence": "high",
      "evidence": [
        "only radius-intersecting change; rule change on the vm's sg 90s before the alert"
      ]
    }
  ],
  "no_confident_candidate": false,
  "degraded": false,
  "narrative": "sg-000108 changed 90s pre-alert and gates the container's vm: committed suspect."
}

The shape to copy: a window that looks quiet at depth 1 is not
concluded quiet until the radius has been walked deeper; when the
mechanism and the exact event line up, commit — flag false, and
confidence as high as the evidence actually earns."""

_IDENTITY = """\
# Identity

You are resgraph-analyst, an incident-triage agent over a resource
graph with full change history. Your entire tool surface is read-only.
Given an alert, identify which recent changes most plausibly explain
it, with evidence a reviewer can verify against the graph and the
event log."""

_TOOL_GUIDANCE = """\
# Tool guidance

- Bracket diff windows tightly around the alert time; widen once if
  the tight window is empty, and say so in the narrative.
- Traversals return bare refs; call fetch_resource only for the few
  that matter.
- Tool calls and tokens are budgeted by the harness. A result saying
  the budget is exhausted means: stop investigating, conclude from the
  evidence you already hold, and set degraded=true.
- Tool errors carry an action field: rephrase means fix your
  arguments, retry means try once more, give_up means conclude
  without that tool."""

_OUTPUT_RULES = """\
Rules:

- suspects are ranked, most plausible first, at most 5.
- Cite only resource ids you actually observed in this run — in the
  alert, the world summary, or a tool result. A cited id that never
  appeared fails validation and is returned to you for correction.
- mechanism_path lists resource ids from the suspected cause to the
  alerting resource. Orientation is strict: for each consecutive pair
  (a, b), resource b must cite a in its own relationships at incident
  time (b runs_on / attached_to / member_of / routes_to a). Never
  write an edge in the reverse direction.
- If the suspected cause is a change to the alerting resource itself,
  mechanism_path is exactly [that resource id] — do not pad the path
  with neighbors.
- no_confident_candidate answers one question: does some change in
  this window actually explain the symptom? On a quiet window the
  correct report sets the flag true and still lists the tempting
  correlates at low confidence, with evidence lines saying why they
  fall short — the worked example below is the shape to copy.
- confidence must track the evidence: high means a direct mechanism
  and the exact event; low means correlation only."""


@dataclass(frozen=True)
class WorldSummary:
    """~500 tokens up front; everything else is ID+fetch through the
    tools. Neighborhood entries arrive pre-rendered as bare-ref lines."""

    resource_counts: dict[str, int]
    neighborhood: tuple[str, ...]
    window_start: datetime
    window_end: datetime


@cache
def prefix_text() -> str:
    discipline = "# Triage discipline\n\n" + load_skill(SKILL_PATH).body.strip()
    schema = json.dumps(TriageReport.model_json_schema(), indent=2, sort_keys=True)
    output_contract = (
        "# Output contract\n\n"
        "When the investigation is done, reply with a single JSON object "
        "and nothing else, matching this schema:\n\n"
        f"{schema}\n\n{_OUTPUT_RULES}"
    )
    return "\n\n".join((_IDENTITY, discipline, _TOOL_GUIDANCE, output_contract, _WORKED_EXAMPLE))


def suffix_text(summary: WorldSummary) -> str:
    counts = ", ".join(f"{t}={n}" for t, n in sorted(summary.resource_counts.items()))
    neighborhood = "\n".join(f"- {line}" for line in summary.neighborhood) or "- (none)"
    return (
        "# World summary (this run)\n\n"
        f"Resource counts: {counts}\n"
        f"Alert resource neighborhood:\n{neighborhood}\n"
        f"Event window: {summary.window_start.isoformat()} .. "
        f"{summary.window_end.isoformat()}\n"
        "Everything else: fetch on demand by id."
    )


def user_text(resource_id: str, symptom: str, fired_at: datetime) -> str:
    return (
        f"Alert: {symptom} on {resource_id}, fired at {fired_at.isoformat()}.\n"
        "Investigate and return the JSON triage report."
    )


def build_prompt(
    *, resource_id: str, symptom: str, fired_at: datetime, summary: WorldSummary
) -> Prompt:
    system: list[dict[str, Any]] = [
        {"type": "text", "text": prefix_text(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": suffix_text(summary)},
    ]
    return Prompt(system=system, user=user_text(resource_id, symptom, fired_at))
