# Pre-mortem — the sentinel L3 classification pass (W4, #254)

Registered spend: <= $0.60 (29 flagged runs + 1 pilot, Opus, ~800
prompt tokens each, 200 out). Halt: any single call > $0.10.

## The causal chain, against merged code

1. The funnel admits 29 runs (measured, `scan.scan_corpus()` —
   src/resgraph/sentinel/scan.py:60).
2. Each flagged run renders a fenced prompt
   (classifier.py:render_prompt) — report truncated at 2k chars, tool
   summary, flags; the payload the L1 rules matched IS inside the
   report field, so the classifier sees the evidence.
3. The pinned judge (evals/models.yaml `opus`, temperature unset on
   anthropic = API default; template hash stamped per verdict) returns
   `TAG:`/`REASON:`; parse_verdict rejects out-of-set tags to
   `unclear` (classifier.py:parse_verdict).
4. Ground truth rides each verdict row (`truth` from the sentinel
   object / benign), so accuracy is computable offline afterwards.

## How could this complete, produce numbers, and measure nothing?

- The prompt could omit the discriminating evidence (e.g. exfil shows
  only in the trace SUMMARY — counts, not breadth). Checked: the
  summary carries per-tool counts (fetch_resource: 40+) and n_calls;
  breadth is visible. Distinct-resource count is NOT in the prompt —
  accepted, noted for the readout.
- All tags could come back `unclear` (format miss) — the pilot gates
  this: one call, parsed tag must be in-set and non-unclear.
- The cap could silently defer everything — cap is 50/day, 30 calls
  planned; deferred rows are marked, and a deferred pilot is a failed
  pilot.

## Pilot gate

One flagged run (`--pilot`), expected: a parsed in-set tag matching
its truth class, cost ~$0.01. Suite only after the pilot verdict is
sane.
