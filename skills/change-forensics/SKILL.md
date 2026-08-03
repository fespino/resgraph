---
name: change-forensics
version: "1.0"
description: Find what changed around the time things broke — ranked suspects with evidence, not verdicts.
scope: resgraph:read
tool_refs: [world_diff, resource_history, blast_radius]
---

## Goal

Answer "what changed around the time things broke?" with a ranked
suspect list, each entry carrying its evidence line — never a verdict.

Constraints before narrative:

- Bracket the window tightly. Do not diff a whole day when the alert
  gives you ten minutes; widen only if the tight window is empty.
- Correlation in the window is rank-ordering evidence, not cause.
  Rank suspects; do not convict them.
- Intersect before you inspect: the diff says what moved, the blast
  radius says what matters — history calls go only to the
  intersection.

## When to use

The user has an incident time (exact or approximate) and wants causes:
"what changed before the 14:32 alert", "why did the api tier degrade
around noon", "did anything move in the last hour".

Not for "what depends on X" questions — that is incident-impact.

## Steps

1. Bracket the incident: T1 = a few minutes before the earliest
   signal, T2 = the alert time. Prefer a 10–15 minute window.
2. `world_diff(from_t=T1, to_t=T2)` → created / deleted / changed
   refs (`one_line` carries the bucket). Empty diff → widen the
   window once, say so in the conclusion.
3. `blast_radius(affected_resource, depth=2, at=T2)` — the set that
   could have hurt the victim at that moment.
4. Intersect: suspects = diff refs ∩ radius refs. Deleted resources
   rank above changed, changed above created, closer-in-radius above
   farther. An empty intersection at depth 2 is not a conclusion:
   deepen — `blast_radius` again with depth=3 — before concluding
   the window quiet. Causes routinely sit two or three dependency
   hops from the victim.
5. `resource_history` on each suspect (usually 2–4): find the exact
   event in the window — its op, sequence, and what the attrs or
   relationships changed from/to.
6. Conclude with the ranked list: each suspect gets one evidence line
   ("deleted at 14:29, three minutes before the alert, inside the
   victim's radius") and a confidence qualifier.

## Tools to call

- `world_diff` — what moved in the window, as bucketed refs.
- `blast_radius` (with `at=`) — what could have affected the victim,
  reconstructed at incident time.
- `resource_history` — the exact change event for each suspect.

## Examples

**"The api tier degraded around 14:32; what changed?"**

1. Window: T1=14:20Z, T2=14:35Z.
2. `world_diff(from_t="2026-08-02T14:20:00Z", to_t="2026-08-02T14:35:00Z")`
   → counts `{created: 3, deleted: 1, changed: 5}`, 9 refs.
3. `blast_radius(resource_id="container-000123", depth=2,
   at="2026-08-02T14:35:00Z")` → 12 refs.
4. Intersection: `vm-000047` (changed), `sg-0009` (deleted).
5. `resource_history(resource_id="sg-0009")` → tombstone at 14:29,
   sequence 88412. `resource_history(resource_id="vm-000047")` →
   attrs `state: running → degraded` at 14:31.
6. Ranked: (1) sg-0009 — deleted 14:29, in the victim's radius,
   security groups gate routing: strong suspect. (2) vm-000047 —
   degraded 14:31, hosts the victim: plausibly a symptom of (1), not
   a cause. Both correlations, not verdicts.

**"Did anything move in the last hour?" (no incident anchor)**

1. Window: the literal last hour, no victim named.
2. `world_diff` only — with no affected resource there is no radius to
   intersect, so report the bucketed counts and the notable refs, and
   ask which service the user cares about before going deeper.

## Anti-patterns

- Diffing a whole day for a ten-minute incident — the suspect list
  drowns in routine churn.
- Convicting the first suspect: "X changed then, so X did it" — rank
  with evidence lines and qualifiers instead.
- Calling `resource_history` on every diff ref instead of the
  intersection — history is the expensive step; intersect first.
- Treating an empty tight window as "nothing changed" without widening
  once and saying the window was widened.
- Concluding "no confident candidate" with most of the tool budget
  unspent and the radius never deepened past 2 — an honest miss is
  acceptable only after the playbook is exhausted, not before.
