# Sentinel prior art

Three sources shape the design; each contributes one load-bearing
idea, and the places we deliberately diverge are stated.

## Clio — insight without reading everyone

[Clio](https://www.anthropic.com/research/clio) (Anthropic) builds
population-level understanding of how an AI system is used without a
human reading individual conversations: aggregate into clusters,
protect individuals, then let reviewers examine patterns with
samples. The load-bearing idea for sentinel is the **posture
inversion at scale**: detection starts as "a human reads the flagged
transcript" and must end as "a human reads a pattern and a sample."
At one reviewer and laptop volume we run the former — every flag is
individually reviewable — and the charter's 1000× section records
the inversion rather than building it. What we adopt now from Clio
is the discipline it implies even at small scale: the reviewer sees
the *triggering evidence*, not the whole haystack, and aggregation
(per-rule precision, per-layer funnels) is a first-class product of
the pipeline, not an afterthought over logs.

## OWASP LLM Top 10 — the seed taxonomy's map

The [OWASP LLM Top 10](https://genai.owasp.org/) names the attack
classes an LLM application actually faces, and the seeded corpus
(W2) maps onto it rather than inventing a private taxonomy:
prompt injection (LLM01) → the **injection** seeds; sensitive
information disclosure / exfiltration shapes (LLM02) → the
**exfil-shaped** seeds; excessive agency (LLM06) → the
**privileged-probe** seeds; unbounded consumption → the
**budget-abuse** seeds. The divergence to state: OWASP's frame is
mitigation checklists; sentinel's frame is *measurement* — each
class exists here as generated items with planted ground truth, so
"mitigated" is a recall number against a corpus, not a checkbox.
The platform's earlier OWASP walk (the LLM06 analysis in D26/D28,
recorded with its two partial rows named) is the precedent: walk
the list, record honestly, measure what can be measured.

## Detection economics — precision is resource allocation

The idea that organizes the whole architecture: a detector's scarce
resource is not compute, it is **reviewer attention**, and every
design choice is an allocation decision against it. This is the
operational lesson of every production alerting and WAF-tuning
practice: a rule at 100% recall and high false-positive rate is not
a safety control, it is a burnout machine that teaches reviewers to
click through — the reflex-approval failure mode the typed-approval
gate (D26) was built against, now at queue scale. Consequences
adopted as requirements, not aspirations:

- **Cost-ordered layers** (D-record pending, W3/W4): the cheap layer
  runs on everything, the expensive layer earns its invocation
  through a flag. Putting the expensive judge first is the named
  pitfall.
- **The benign false-positive rate is the headline metric** — recall
  on attacks is the easy half; the confusion matrix is reported on
  the combined stream (W2's benign corpus exists for exactly this).
- **Cost-per-true-detection** is the phase's closing number
  (BENCHMARKS discipline: methodology and hardware attached),
  because a detection program that cannot state what a catch costs
  cannot argue for or against its own existence.
- **Scoped exclusions, never rule disables** (W5): the response to a
  false positive is a surgical carve-out with a test, because
  disabling a rule to silence one alert is how detection quietly
  goes blind — the same lesson as the eval gate's "a control that
  silently stops covering something is worse than no control."
