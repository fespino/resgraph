# The detection corpus

Sentinel is measured on a combined stream with two halves; a detector
judged on only one half is judged on nothing (W2, #252).

- **The benign half** — every committed *normal* triage run, replayed
  through the detector by reference. This is the false-positive
  denominator, and it is the headline: a detector that catches every
  attack while flagging a fifth of normal traffic is a
  reviewer-burnout machine, not a control.
- **The attack half** — seeded runs with planted malice and ground
  truth. This is the recall numerator.

## Benign: named, not copied

`evals/sentinel/benign-manifest.json` names the run files whose rows
are all normal triage; `corpus.iter_benign()` reads them by
reference. The run files stay the system of record — the manifest is
a selection over them, never a copy that could drift. A file is
excluded if any row carries a companion or induced-fault tag
(`store_degraded`, `budget_starved`, `injection`, `coverage_gap`,
`reskin`, `fault:*`): those runs are deliberately abnormal and are
not benign traffic. A test asserts the committed manifest equals a
fresh selection, so a new companion run cannot silently enter the
benign pool.

Today: 18 run files, 361 benign rows, spanning the phase-8 base runs
and the phase-10 model arms (Haiku/Opus/Sonnet) — benign traffic
across workers, which W3 must treat as a behavior-profile
consideration (per-worker baselines, not one global one).

## Attacks: the generator plants the malice

Seeded attacks are deterministic mutations of committed benign rows
— the D25 discipline (the generator plants the cause) applied at the
run level, to malice. Each attack stays a real run in every
dimension except the planted one and carries its ground truth in a
`sentinel` object (`id`, `malicious: true`, `attack_type`, `planted`,
`base_run`). Five per type, four types mapped to the OWASP LLM Top 10:

| type | OWASP | what is planted |
|---|---|---|
| `injection` | LLM01 | a sentinel-marked steering instruction appended to the report narrative |
| `exfil_shaped` | LLM02 | a 40–60 wide `fetch_resource` fan-out — whole-world enumeration dressed as blast radius |
| `budget_abuse` | unbounded consumption | a 30-call re-fetch loop + inflated output tokens |
| `privileged_probe` | LLM06 | a failed `apply_remediation` call (D26's structural block makes it an *attempt* signal) |

## Two properties the tests hold

- **Deterministic rebuild.** The base run for each item is chosen by a
  seed derived from the item id, so `resgraph-sentinel corpus build`
  reproduces `attacks.jsonl` byte-for-byte. The committed corpus is
  drift-guarded by a rebuild test — a change to the generator that
  moves the bytes fails CI until the corpus is regenerated and
  reviewed as a diff.
- **Sanitized by construction.** The one payload attacks may carry is
  a fixed, sentinel-marked template (`[SENTINEL-SEED] …`). Tests
  assert only injection items carry the marker and the benign corpus
  never does — the corpus cannot become a channel for un-swept text
  (the SANITIZATION check-8 shape).

## Regenerating

```
resgraph-sentinel corpus build                  # rebuild attacks from the manifest
resgraph-sentinel corpus build --refresh-manifest  # re-scan evals/runs for the benign set first
resgraph-sentinel corpus stats                  # the combined-stream shape
```

Both files are committed; regeneration is a reviewable diff, not a
runtime step.

## The layers' first measurement (W3)

`resgraph-sentinel scan` on the committed corpus: layer 1 catches
20/20 seeded attacks with zero benign flags (each rule's tp/fp
reported individually); layer 2 flags the volumetric attacks and 9/361
benign rows (~2.5%, the Tukey-fence novelty channel); the funnel
admits 29/381 runs to layer 3. The division of labor is measured, not
designed: text/insert attacks are invisible to features (layer 1's
job), volume is invisible to signatures (layer 2's). Decision record:
D37; floors asserted in tests/test_sentinel_layers.py.
