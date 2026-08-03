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
    "world summary",
    "alert payload",
)

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
- On a quiet window, no_confident_candidate=true with an empty
  suspects list is a complete, correct answer — accusing something is
  not.
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
    return "\n\n".join((_IDENTITY, discipline, _TOOL_GUIDANCE, output_contract))


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
