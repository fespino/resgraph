# resgraph MCP server

Read-only investigation tools over a resource graph with full history:
a live graph store answers "what depends on what right now"; an
append-only event log answers "what was true at time T". Targets MCP
spec revision `2026-07-28`; every tool is a single-shot read (no
session state, no handles).

## Tools

| Tool | Question it answers | Store |
|---|---|---|
| `blast_radius` | what breaks if this resource dies (live or at time T) | hot / composite |
| `dependency_path` | why does A depend on B — one shortest path | hot |
| `resource_history` | every recorded change to one resource, oldest first | cold |
| `world_diff` | what was created/deleted/changed between T1 and T2 | cold |
| `fetch_resource` | full detail for any resource by id (live or at T) | hot / cold |

## Budget semantics

- Traversal and diff responses carry **bare refs** `{id, type,
  one_line}`, not full payloads — call `fetch_resource` for the few
  that matter; do not fetch every ref.
- Every response serializes under a **hard token cap**. Overflow sets
  `truncated: true`, keeps `total_count` honest, and puts the next move
  in `pagination_hint` prose ("call again with offset=N"). A truncated
  radius is "at least N", never "N".
- `depth` beyond the platform cap is **clamped, not rejected** —
  `depth_clamped: true` says so.
- Every response carries `fetched_at` and `source` (`hot` | `cold` |
  `composite`). If the world churns, a stale answer is a wrong answer:
  re-fetch rather than reasoning over old payloads.

## Filter grammar

`blast_radius` accepts `filter`: an AND-chain of comparisons — `type=vm
AND attrs.zone=z1 AND attrs.cpu>=4`. Ops `= != < <= > >=`; ordering ops
need numeric values; `type` supports only `=`/`!=`; no OR, no
parentheses. Malformed filters answer with the correction in the error
message.

## Scopes and risk

All five tools declare `resgraph:read`; annotations on every tool:
read-only, non-destructive, idempotent, closed-world. Per-tool `_meta`
carries `timeout_s` and `error_actions` (what to do on failure:
rephrase / retry / give up).

## What this server will NOT do

- No writes of any kind — nothing here mutates the platform.
- No unbounded queries — depth caps, token caps, and pagination are
  enforced server-side, not requested politely.
- No authority from the caller — scopes are transport-injected;
  nothing in a tool schema lets the model claim its own permissions.

## Prompts

Two investigation playbooks ship as MCP prompts: `incident-impact`
("what breaks if X dies?") and `change-forensics` ("what changed around
the time things broke?"). Load one before an investigation — they
encode the budget discipline above.
