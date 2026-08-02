---
name: incident-impact
version: "1.0"
description: Assess what breaks if a resource dies — impact radius, critical dependents, single points of failure.
scope: resgraph:read
tool_refs: [blast_radius, fetch_resource, dependency_path]
---

## Goal

Answer "what breaks if X dies?" with a bounded, evidence-backed impact
summary: the affected set, the 2–3 dependents that actually matter, and
the single point of failure if there is one.

Constraints before narrative:

- Never fetch every ref a traversal returns. Budget: at most 3–5
  `fetch_resource` calls per investigation, on the refs the user cares
  about.
- A truncated radius is "at least N", never "N".
- Check `fetched_at` on every payload you reason over; if the
  investigation spans minutes, re-fetch before concluding.

## When to use

The user names a concrete resource and asks about impact, risk, or
consequences of failure: "what happens if db-42 dies", "how risky is
restarting host-7", "who depends on lb-3".

Not for "what changed" questions — that is change-forensics.

## Steps

1. Confirm the subject exists and is current: `fetch_resource(X)`.
   `found=false` ends the investigation with that answer. Note
   `fetched_at`.
2. `blast_radius(X, depth=2)`. Depth 2 first — widen to the cap only
   if the boundary consists of pass-through types (lb, sg) whose own
   dependents are the real impact.
3. If `truncated=true`: report "at least total_count affected" and
   follow `pagination_hint` only when the user needs the full roster,
   not to satisfy completeness instinct.
4. `fetch_resource` ONLY the refs the user's question is about
   (their service, their tier) — 3–5 calls, no more.
5. For the 2–3 most critical dependents, `dependency_path(dep, X)` —
   the edge types on the path are the evidence line ("runs_on, so a
   hard dependency, not a routing preference").
6. Conclude: one paragraph — affected count (exact or "at least"),
   the critical dependents by name, and the single point of failure
   if every path funnels through one node.

## Tools to call

- `blast_radius` — the affected set as refs. Start depth=2.
- `fetch_resource` — detail on the few refs that matter.
- `dependency_path` — the why behind each critical dependent.

## Examples

**"What breaks if db-42 dies?"**

1. `fetch_resource(resource_id="db-42")` → found, `fetched_at` noted.
2. `blast_radius(resource_id="db-42", depth=2)` → 14 refs,
   `truncated=false`.
3. Refs are 11 containers, 2 vms, 1 lb. User asked about the API tier:
   `fetch_resource(resource_id="container-000123")` → attrs show
   `tier=api, state=running`.
4. `dependency_path(from_id="container-000123", to_id="db-42")` →
   `path=[container-000123, vm-000047, db-42]`,
   `rels=[RUNS_ON, ATTACHED_TO]`.
5. Summary: "14 resources are affected (complete, not truncated). The
   api tier reaches db-42 through vm-000047 — that vm is the single
   point of failure: all 11 containers route through it."

**"How bad is losing host-7?" (hub case)**

1. `fetch_resource(resource_id="host-7")` → found.
2. `blast_radius(resource_id="host-7", depth=2)` → `truncated=true`,
   `total_count=612`, hint says next offset.
3. Report "at least 612 affected — this is a hub". Do NOT page through
   all 612; instead fetch the 2 refs matching the user's service and
   path them.

## Anti-patterns

- Fetching every ref returned by `blast_radius` — that is the blown
  context window the refs exist to prevent.
- Reporting a truncated radius as complete. `total_count` with
  `truncated=true` means "at least", say "at least".
- Widening depth because the answer "feels small". Widen only on
  pass-through boundary types.
- Reasoning over a payload fetched before a long pause without
  re-fetching — the world churns; `fetched_at` is there to be checked.
