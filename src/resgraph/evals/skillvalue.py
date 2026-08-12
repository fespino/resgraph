"""The paired skill arm: what the change-forensics playbook actually
buys, ledgered available -> retrieved -> invoked -> relevant.

"The skill was loaded" and "the skill did the work" are different
claims. A with-skill pass is credited to the skill only when the
trajectory shows the skill's method; an item both arms pass is scored
by cost, not counted as a skill win.

Two limits, stated where the numbers are read:
- retrieved collapses into available. The skill lives statically in the
  prefix, so if it was available it was in context — this architecture
  cannot separate the two, and the ledger says so rather than inventing
  a number.
- invoked is a heuristic read of a trajectory, not a certainty. It now
  scores the method's SHAPE from the call arguments — a tightly
  bracketed diff window and a blast radius reconstructed at incident
  time — rather than the mere presence of two tool names; sharper, not
  perfect.
"""

from collections import defaultdict
from datetime import datetime
from typing import Any

from .report import item_passed

# The playbook wants a tight incident window (it says 10-15 min); a diff
# spanning more than this is the "diffed a whole day" anti-pattern.
TIGHT_WINDOW_S = 3600.0


def _calls(row: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [c for c in row.get("tool_trace", []) if c["tool"] == name]


def _tight_diff(call: dict[str, Any]) -> bool:
    args = call.get("args") or {}
    try:
        span = datetime.fromisoformat(args["to_t"]) - datetime.fromisoformat(args["from_t"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 < span.total_seconds() <= TIGHT_WINDOW_S


def _reconstructed_radius(call: dict[str, Any]) -> bool:
    return (call.get("args") or {}).get("at") is not None


def skill_invoked(row: dict[str, Any]) -> bool:
    """The method's shape, not its tool inventory: a tightly bracketed
    world_diff and a blast_radius reconstructed at incident time — the
    playbook's two load-bearing instructions, both read from the call
    arguments. (Bounding history to the intersection is a further
    refinement the trace cannot cheaply confirm.)"""
    return any(_tight_diff(c) for c in _calls(row, "world_diff")) and any(
        _reconstructed_radius(c) for c in _calls(row, "blast_radius")
    )


def _by_item(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["scenario_id"]].append(row)
    return grouped


def _item_passed_all(trials: list[dict[str, Any]]) -> bool:
    return all(item_passed(r) for r in trials)


def _item_invoked(trials: list[dict[str, Any]]) -> bool:
    return sum(skill_invoked(r) for r in trials) * 2 > len(trials)


def skill_value(
    with_rows: list[dict[str, Any]], without_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    with_items = _by_item(with_rows)
    without_items = _by_item(without_rows)
    shared = sorted(set(with_items) & set(without_items))
    comparable = set(with_items) == set(without_items)

    relevant, credited_not_invoked, both_pass = [], [], []
    invoked = 0
    for sid in shared:
        w, wo = with_items[sid], without_items[sid]
        w_pass, w_invoked, wo_pass = (
            _item_passed_all(w),
            _item_invoked(w),
            _item_passed_all(wo),
        )
        if w_invoked:
            invoked += 1
        if w_pass and not wo_pass:
            (relevant if w_invoked else credited_not_invoked).append(sid)
        elif w_pass and wo_pass:
            both_pass.append(sid)
    return {
        "comparable": comparable,
        "items": len(shared),
        "available": len(shared),
        "retrieved": len(shared),
        "invoked": invoked,
        "relevant": sorted(relevant),
        "uncredited_wins": sorted(credited_not_invoked),
        "both_pass": sorted(both_pass),
    }


def render(value: dict[str, Any]) -> str:
    lines = [f"skill arm: items={value['items']} comparable={value['comparable']}"]
    lines.append("  available  {:>3}  (in the prompt)".format(value["available"]))
    lines.append("  retrieved  {:>3}  (== available; static prefix)".format(value["retrieved"]))
    lines.append(
        "  invoked    {:>3}  (tight diff window + radius reconstructed at incident time)".format(
            value["invoked"]
        )
    )
    lines.append(
        "  relevant   {:>3}  (invoked, with-skill passed where without failed)".format(
            len(value["relevant"])
        )
    )
    if value["uncredited_wins"]:
        lines.append(
            f"  NOT credited: {len(value['uncredited_wins'])} with-skill wins where the "
            "skill was not invoked (the pass was not the skill's)"
        )
    lines.append(f"  both arms passed: {len(value['both_pass'])} (score by cost, not a skill win)")
    return "\n".join(lines)
