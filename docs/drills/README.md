# Drill runbook

How this repo runs induced-failure drills: what has to be written down before one runs, what gates it, and what gets published after.

The rules here were paid for. [INC-001](../incidents/INC-001-hotstore-loss.md) killed the hot store under load and worked first time. [INC-002](../incidents/INC-002-degraded-drill-misfire.md) spent **$5.88 across two runs that measured nothing**, produced a confidently wrong diagnosis in between, and would have published a false finding if one grader rule had been absent. Every step below exists because one of those two runs needed it.

## When a drill is the right instrument

A drill induces a failure to test a claim the system makes about itself. Reach for one when:

- the claim is about **behavior under partial failure**, which unit tests cannot reach;
- the failure is **inducible and reversible** at laptop scale;
- and there is a **number** at the end, not just a vibe — "the DLQ stayed flat", "found-rate fell 7pp", "detection took 172s".

If you cannot name the number in advance, you are not ready to run.

## The sequence

```
1. design          what claim, what fault, what number
2. causal chain    each link cited to file:line
3. pre-mortem      "how could this produce numbers and measure nothing?"
4. pilot           smallest falsifying case, ~$0.15
5. run             the suite
6. postmortem      including when it worked
```

Steps 2–4 are the ones INC-002 skipped.

### 2. The causal chain, cited

Write the path from fault to observation, and put a `file:line` on every arrow:

```
kill hot store        → faults.py:29 raises on hot acquisition
→ a tool that reads it fails    → entity.py:35  require("hot")   ← ONLY when at is None
→ the agent sees an error       → tools.py:100  ok=False outcome
→ the report says so            → graders.py:92 degraded dimension
```

The arrow you cannot cite is the one that will break. In INC-002 it was arrow two: `fetch_resource` and `blast_radius` are hot **only when `at is None`**, and triage of a past alert passes `at=<fired_at>`, so both read cold. One grep — `grep -rn 'require(' src/resgraph/tools/canonical/`, five hits — would have shown it.

**Trace the chain for the workload you will actually run, not in the abstract.** "The agent uses the graph" was true generally and false for this workload.

### 3. The pre-mortem

The eval protocol already requires an adversarial pre-mortem for prompt iterations ("how could the model satisfy this change's letter without the intended behavior?"). Drills get their own question:

> **How could this run complete, produce numbers, and measure nothing?**

Answer it in writing, in `docs/drills/premortem-<name>.md`, from [the template](premortem-template.md), before the first paid run. List the ways, then say which are checked and how.

### 4. The pilot

Run the smallest case that could falsify the premise — normally **one item at k=1** — and assert the fault fired, before spending on the suite. At current prices that is about **$0.15 against $3**.

Write the pilot into the drill script as a gate, not a habit. `scripts/drill-analyst-degraded.sh` refuses to run the suite when the pilot shows no failed tool call.

### 6. The postmortem

Every drill gets one, from [the template](postmortem-template.md) — **including when it works**. INC-001 succeeded and is still the most useful document from that phase, because it recorded the budget arithmetic and what broke before the first clean run.

Numbered `INC-NNN` in `docs/incidents/`, in the order events happened. An instrument failure earns a number as readily as a system failure: the measurement layer is production for a project whose claims are its output.

## Hard rules

- **A paid run is a deploy.** Steps 2–4 are not optional because the amount is small.
- **Assert that the fault fired.** Make it a graded dimension, not an inference from results. An item whose induced fault never fired must **fail**, not pass quietly — this is the only reason INC-002 was caught rather than published.
- **Record what happened at the fault boundary.** Per-tool outcomes in the run row (`tool_trace`) turn the next diagnosis into evidence instead of inference. Instrument the drill before running it, the same rule the platform applies to its subject.
- **Correct estimates before the run, in their own commit.** An estimate fixed after its run is not an estimate.
- **Do not remediate on an unverified diagnosis.** A cause consistent with the evidence is not the cause operating. Prove it on a pilot before paying to re-run.
- **Never report a number without its method and hardware** (D4 — benchmarks carry methodology). Run rows carry `git_ref`, host class and store digests, so the version that produced a number is recoverable from the number.

## What to write down when it goes wrong

Keep the wrong diagnosis in the postmortem, labelled. A note that records only the correct explanation teaches nothing about how to reach one — and the wrong one usually had every property of a good diagnosis except a test.

Publish the assumption audit too, including the assumptions that were right but weakly tested. "Fabrications 0" from a run where nothing failed is not evidence of honesty, and saying so is the difference between a postmortem and a press release.
