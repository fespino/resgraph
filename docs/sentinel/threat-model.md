# Sentinel threat model

Written before the detector (#251), so the layers defend the doors
that matter rather than the ones that were easy to wire. Two halves:
the **behavior half** (what the agent itself might do or be steered
into — sentinel's subject, #250) and the **serving half** (the stack
around it). Decision record for the one enforced-vs-named call made
here: D35.

## Assets, ranked

1. **API keys and spend.** The one asset whose loss costs money
   immediately. Held in the environment only, never in artifacts
   (D27's no-secrets rule); every spender has an enforced budget
   (D29a, the gateway caps).
2. **The audit trail and eval evidence.** The platform's product is
   its receipts: committed runs, baselines, the tamper-evident trail
   (D27). Corrupting them quietly is worse than deleting them loudly.
3. **Host integrity.** The laptop is the platform; a compromised
   serving stack is a foothold on it.
4. **Store contents.** Ranked last on purpose: the worlds are
   synthetic and regenerable by seed (D6). The stores matter as a
   pivot surface, not as data.

## Adversaries

- **The LAN opportunist** — scans whatever network the laptop joins.
  Largely retired: every listener binds loopback (#244, #246), and
  the services' unauthenticated posture is honest only because of it.
- **The hostile-content author** — plants instruction-shaped payloads
  in world data (resource attributes, alert text) to steer triage.
  The behavioral half's main adversary; structurally contained (D26:
  the privileged tool is absent from the agent's surface; D28: model
  output is never an instruction) and measured behaviorally (the
  injection eval slice).
- **The supply chain** — a poisoned image, dependency, or model
  artifact. Pinned by digest end to end: images and Actions (phase
  0), weights (#248), prompts and fed context by fingerprint (D23,
  D34).
- **The misbehaving agent itself** — over-fetching, budget burn,
  probe-shaped tool use, whether steered or emergent. This is
  sentinel's subject (#250, W2–W5).
- **The honest-mistake operator** — approves on reflex, tunes a
  threshold wrong. Typed approvals with recorded time-to-decision
  (D26) and the CI recall floor (W5) exist for this adversary.

## Trust boundaries

1. **World data → agent:** everything the tools return is untrusted
   content the model reads. Injection is contained, not solved.
2. **Model output → harness:** never an instruction. The report has
   no remediation field; execution is operator-assembled (D28).
3. **Process → network:** every listener loopback-bound (#244/#246).
   Outbound is the accepted residual — see D35: the compose-level
   egress control was piloted (`scripts/egress-pilot.sh`) and no
   mechanism on this platform blocks container egress without also
   killing the published ports the consumers need. Rejected on
   measurement, with reversal conditions.
4. **Artifacts → history:** evidence is committed; the audit chain is
   tamper-evident with its residual named (tail truncation, D27).

## STRIDE-lite over the four surfaces

| Surface | Main threats | Standing controls | Gaps → workstream |
|---|---|---|---|
| Serving stack (stores, ollama, gateway, metrics) | tampering (poisoned artifact), DoS (spend, hot loops), info disclosure (open ports) | digest pins incl. weights (#248), loopback binds, admission control + Retry-After, budgets (#228), probe opt-in | breach *signals* below; egress residual (D35) |
| Agent loop | elevation (injection → privileged act), spoofing (fabricated evidence) | D26/D28 structural boundary, fabrication-blocking evals, injection slice | continuous detection = sentinel L1–L3 (W3/W4) |
| Eval/CI pipeline | tampering with evidence, gamed graders | committed runs, fingerprints, the D29b gate, mutation-tested graders | detector quality gets the same gate (W5) |
| Operator surface | repudiation (unlogged decisions), reflex approvals | typed counts, approval-as-audit-record, grant expiry (D26) | review-queue ergonomics + rubber-stamp detector (W5) |

## Serving-side breach signals — named, with their status

The kimi-audit question "how do I know I'm breached?" answered at
laptop scale. Cheap signals ship as checks that already exist;
the rest are named so their absence is a decision, not an oversight.

| Signal | Status |
|---|---|
| Model weights changed under the tag | **shipped** — digest resolved at every run start; declared mismatch refuses (#248) |
| Audit chain broken | **shipped** — `resgraph-analyst audit <run> --verify` names the first broken row (D27); run it in any incident |
| Spend anomaly | **shipped** — judge breaker (D29a), fall-forward budget (#228), cost-per-task SLO (D33); per-run budget rules join sentinel L1 (W3) |
| Context window drifted from declaration | **parked** — #249, declare-then-verify against the loaded model |
| Unexpected listener on the host | **named, not built** — the expected set is small (six compose ports + gateway + API + metrics, all loopback); a snapshot check is cheap but host-level, outside sentinel's run-scoring; build if an incident ever wants it |
| Unexpected container egress | **named, not enforced** — D35: no workable compose mechanism on this platform; revisit on its reversal conditions |

## What this deliberately does not cover

Sandboxing the serving stack (containers → microVMs) and code
execution for the agent — a future phase's subject; the boundary
here is detection and posture, not isolation. Population-level
privacy techniques upstream of review are recorded in the charter's
1000× section, not built at one reviewer.
