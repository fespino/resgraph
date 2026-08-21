# Cross-layer consistency review (2026-08-21)

Method: pick a concern, ask **every** layer what its policy is, record
where they differ, and mark each difference *decided* or *drifted*.
Opened by #329, whose premise came out of mini-phase 13.5: that phase
found the platform holding two opposite policies on memory, neither
of them chosen, and no per-layer review could have seen it — the
question has to span layers to exist at all.

Scope: five concerns (memory, time, provenance, refusal, identity)
across every package in `src/resgraph/`. Findings only; follow-ups are
opened for the drifted ones and named at the end. $0 — reading.

A note on what "drifted" means here. It is not a synonym for wrong.
Most of these differences are locally reasonable, which is exactly why
they survived: each was decided by someone solving one layer's problem
well. *Drifted* means the difference between layers was never the
subject of a decision — nobody compared them and chose.

## Concern 1 — memory: what does each layer forget, and when?

This is the concern #329 was opened with, already confirmed. The walk
found five policies, not two.

| Layer | Policy | Ages by | Decided? |
|---|---|---|---|
| Dispatch latency stats (`gateway/dispatch.py:26,61-65`) | rolling 300 s window; samples outside it are dropped | wall clock, continuously | **Decided** (D41) |
| Dispatch error window (`gateway/dispatch.py:27,95-106`) | rolling 30 s window | wall clock, continuously | **Decided** (D41) |
| Quality table (`gateway/quality.py:107`, `server.py:89,943`) | never forgets; staleness > 90 days is announced at load, once | announced, never applied | **Decided** (D51) |
| Response cache (`gateway/cache.py:25,55,65`) | 900 s TTL, plus LRU eviction at capacity | wall clock at read | **Decided** (D32-family) |
| Sentinel baseline (`sentinel/profile.py:88-95`) | no aging at all — mean/stdev over the whole benign corpus | never | **Drifted** |
| Institutional memory (`evals/context.py:16-23`) | manual: the working set is what sits between the markers | a human editing EVALS.md | **Decided** (D34) |
| Market snapshots (`gateway/market.py`) | keeps everything, no retention rule | never | **Drifted** (#332) |

**The pair 13.5 found is now decided on both sides.** D41 forgets by
construction — an idle backend returns to *unmeasured* rather than
coasting on a stale estimate — while D44's table forgot nothing. D51
resolved it, and the resolution is the interesting part: the two
layers *should* differ, because serving a request updates the latency
window it is ranked by, and serving a request produces no pass^k. A
pull is an observation at one layer and not at the other. So the
difference is real and now argued, rather than incidental.

That is the pattern worth extracting: **the test is not whether two
layers agree, it is whether the difference was reasoned about.**

**F1 (drifted) — the sentinel baseline never ages.** `build_baseline`
computes mean and standard deviation over the entire benign corpus
(`sentinel/profile.py:88-95`) with no window and no decay. Every other
statistical estimator in the platform is windowed. The behaviour of
the models being profiled changes with every prompt edit, model
upgrade, and skill change — the same non-stationarity D41's rolling
window exists to handle, one layer over. The corpus is refreshed by
regenerating it, which is a human action on no schedule, so "how old
is this baseline" currently has no answer in the system.

This is not urgent: the baseline is rebuilt whenever the corpus is,
and detection thresholds are re-derived with it. It is filed because
the *question* has no answer, not because the number is known wrong.

**F2 (drifted, known) — market snapshots have no retention rule.**
Already open as #332; recorded here so the concern's row is complete.
The collector now appends one file per day to the `market-data`
branch (D46, second amendment) and nothing ever removes one.

## Concern 2 — time: which clock does each layer record?

D13 gives the platform two clocks — **event time** (when the world
changed) and **observation time** (when the pipeline saw it) — and
rejects Iceberg's commit-time travel as the as-of mechanism precisely
because the two drift apart under backfill, replay, and lag. The
query layer honours the split explicitly: every response carries both
`at` (event time, caller-supplied) and `fetched_at` (observation
time), plus the store that answered (`api/app.py:163-164`).

Twenty-plus layers were walked. Most are individually fine. Three
findings, in ascending order of consequence.

**F3 (drifted) — the ingest pipeline (D48) records one clock, and
destroys the other.** `observations.ts` is the event's own timestamp
(`ingest/worker.py:44`), and `sink.py:19-32` has no column that could
hold an observation time. The pipeline *does* capture one:
`RefQueue.enqueue` stamps `enqueued_at` (`ingest/spool.py:66`) — and
`ack` deletes the row on success (`spool.py:83-85`), so the stamp is
destroyed at the moment the sink row is written. `enrich()` never
sees it: `drain()` reads the batch and discards the ref
(`worker.py:58-61`).

Consequences, stated plainly: ingest lag is unanswerable, "what did
the sink know at time T" is unanswerable, and **a replay
(`worker.py:65-72`) is indistinguishable from live ingest by any
stored value** — which is exactly the property D13 built the
distinction to preserve. D48 (`SPEC.md:2748-2787`) argues raw-first,
at-least-once, idempotency and enrichment, and never mentions a
clock. This is drift, not a decision.

The wrinkle that makes it a real decision rather than a patch: the
spool is content-addressed (`spool.py:38`), so a receive timestamp
cannot live in the raw file without changing its hash and breaking
dedup identity. The observation clock therefore belongs on the queue
row or the sink row, and choosing which is the decision to take.

Worse, `enrich` puts two different clocks in adjacent columns without
naming either: `ts` is event time (`worker.py:44`) while
`run_started_at` comes from the audit run row, which is recording
wall clock (`analyst/audit.py:87`). `synth_batch` generates both from
one fictional timeline (`worker.py:78-87`), so no test can surface
the mixture.

**F4 (drifted) — the audit trail's `ts` means different things to its
two readers.** It is written as recording wall clock
(`analyst/audit.py:179` → `:290`); the OTLP exporter reads it as
event time for span starts (`langfuse/otlp.py:51,74-75`); the silence
detector reads it as observation time (`ingest/reconcile.py:53-59`).
The silence detector is *correct by accident* — it compares `MAX(ts)`
against `time.time()`, which is sound only because that column
happens to be wall clock. Both readings are load-bearing today, and
neither is written down.

**F5 (drifted, and the sharpest thing in this review) — two
incompatible timelines share one column in the cold store.** The
generator emits `event_time` on simulated world time starting at
`WORLD_EPOCH = 2026-01-01` (`gen/churn.py:12,40,71`). The
remediation executor emits `UpdateMessage(event_time=self.now())`
where `now` defaults to real wall clock
(`analyst/executor.py:126,92`). Both reach the same stream — the
executor's `emit` is wired to `RedisSink.emit_many`
(`analyst/cli.py:391`, `gen/sinks.py:33`) — and therefore the same
cold `events.event_time` column (`cold/store.py:104`).

`state_at(T)` (D13) resolves state by highest sequence with
`event_time <= T`. With a world sitting in January and remediation
writes stamped August, **every remediation event sorts after every
generated event regardless of what actually happened**, and an as-of
query for a January timestamp cannot see a remediation at all. The
platform's headline capability reads one column that carries two
clocks.

Two things keep this from being an outage. The executor's `now` is
injectable and tests inject it (`executor.py:92`), and remediation to
a live stream is an operator path that the eval harness does not
exercise. Neither is a defence of the default.

The same defaulting appears once more: `analyst/cli.py:358` defaults
an alert's `fired_at` to `datetime.now(UTC)` when the flag is
omitted, putting the alert months ahead of every event in a seeded
world.

**Decided and worth recording as such:** the hot store carries **no
timestamp at all** (`graph/ingest.py:93-95,219-221`) — ordering is
`applied_seq` only, and every time question routes to cold
(`api/app.py:217`). That is D3 and D13 agreeing, and it is the
cleanest boundary in the platform. Eval run rows likewise carry no
timestamp field (`evals/runner.py:465-486`): only `run_id` (a
wall-clock string) and `latency_s` (a duration). D46 explicitly
defers the question for market snapshots — a catalog has no event
time distinct from when it was fetched (`SPEC.md:2694-2699`), which
is a decided absence rather than a missing column.

## Concern 3 — provenance: what does each layer require before it trusts a number?

Two layers set the bar. The quality table refuses a score without
`run`, `date` and `fabrication_count` — *a score without provenance is
an opinion* (`gateway/quality.py:37-42`, D44/D52). Market snapshots
refuse without `source` and `fetched_at` (`market.py:97-102`, D46).
The institutional-memory store is a third: it refuses to feed an
unmarked file, and the proposal artifact stamps the fed text's sha256
(`evals/context.py:22`, D34).

The walk found the strongest gap sits directly underneath the layer
that was hardened.

**F13 (drifted) — the router's hard provenance requirement is
satisfiable by a filename.** `quality.py` refuses an entry lacking
`run` and `date`. Those values are minted at `evals/cli.py:316,324`:
`run` is **whatever path string the operator typed on the command
line**, and `date` is sliced out of `rows[0]["run_id"]`, falling back
to `Path(path).stem` when row 0 has no run id. Neither is checked
against the run file's own envpin — and `git_ref` is on every row
(`runner.py:370-372`) and dropped at this boundary.

So the reader's guarantee rests on the writer's unvalidated string. A
strict consumer above a lax producer is the recurring shape of this
whole review: the requirement is real, the thing it requires is not
checked at the only point where checking is possible.

**F14 (drifted) — `evals/baseline.json` is the least-provenanced
number in the repository that anything blocks a merge on.** It is
`aggregate(rows)` verbatim (`evals/cli.py:174-177`): `model`,
`item_ids`, `fingerprints` and nothing identifying its origin — no
run id, no git ref, no date. The command does not call `verify_run`;
the docstring advises it (`cli.py:172`), which is a convention, not a
control. A run that failed the fabrication halt can become the bar the
gate defends. The gate then compares only `model`
(`gate.py:181-188`) and **never reads `fingerprints`**, though the
aggregate carries them — so a baseline captured under a different
prompt shape gates silently.

**F15 (drifted against an explicit decision) — the sentinel stamps
half of what D38 requires.** D38's text is unambiguous: *setup **and**
template hash stamped per verdict*. `Classification` carries
`template_sha` and no model field (`sentinel/classifier.py:104-110`);
the judge model is passed to `classify` (`cli.py:139`) and echoed to
the terminal (`cli.py:143`), never stored. Downstream is worse: the
two committed label files carry `{rule, run_key}` and
`{run_key, evidence}` (`queue.py:127-145`), dropping reviewer and
timestamp — so the labels D39 calls "the product" ship without an
author, whose identity survives only in an uncommitted local sqlite
file.

This is the one finding in the review that is not drift between two
reasonable positions. A decision was written and half-implemented.

**F16 (drifted) — BENCHMARKS.md has no mechanism at all.** The rule
that no number ships without hardware and methodology is stated three
times in prose (`BENCHMARKS.md:3-4`, `CLAUDE.md:26`, `INDEX.md:8`) and
enforced nowhere: no test, no workflow, no link from a table row to
the script in `benchmarks/` that produced it. It is worth naming
precisely because D52 argued the opposite for a smaller stake — a
convention is not a control — and this is the repository's most
public claim.

**Two more absence-reads-as-zero survivals**, the shape D50 named and
D52 cited. `ingest/worker.py:46` prices an event with
`str(run.get("model") or "")`, which maps to `0.0`
(`evals/pricing.py:18-19`), so a row from an unidentified producer
lands in `observations.cost_usd` as a legitimate-looking zero and
`layouts.py:64-72` sums it. And `langfuse/reconcile.py:32,37` skip
their comparisons when the far side returns `None`, so a row missing
latency or tokens reconciles clean — in the acceptance test whose
entire job is catching that.

**And D52's own manifests do not cover every boundary.** They classify
`aggregate` → `arm_summary` → quality table. Uncovered: `aggregate` →
**gate** (where `fingerprints` reaches a consumer that ignores it),
run rows → `baseline.json`, and the served response → usage ledger,
where request id, backend, routing source and fallback chain all exist
on the object (`server.py:1163-1171`) and are dropped
(`accounts.py:89-119`). The decision was right and its application was
partial — which is itself an instance of the pattern this review keeps
finding.

Recorded on the decided side: the audit store requires `model` and
`started_at` NOT NULL with a recomputable per-event chain hash
(`analyst/audit.py:41-68`); dispatch reads an empty window as `None`
because *an unmeasured backend is a fact, not a zero*
(`dispatch.py:53-58`, D41); the L2 baseline refits from the committed
corpus on every scan, so there is no stored artifact to go stale
(`sentinel/profile.py:84-104`); and the registry deliberately **admits**
on an undeclared capability — the inverse default of "no eval, no
route" — with its reversal condition in the docstring
(`registry.py:52-58`). That last pair is the good version of this
review's subject: two layers with opposite defaults, both argued.

## Concern 4 — refusal: how does each layer say no?

The gateway is the strict layer and knows it: 400/401/402/403/410/429/
501/502/503, each with an `outcome` label, and D31 argues the sharpest
pair explicitly — `budget_503` must not read as `exhausted_503`,
because "we refuse to pay past the cap" is not "everything is down"
(`gateway/server.py:409-424`). The eval gate is the second strict
layer: exit 0 passed / 1 blocked / 3 declined / 4 evidence unreadable,
with D29b's reasoning stated — *separate the four so CI never infers a
verdict by grepping prose* (`evals/cli.py:225-268`).

Those two are the standard. Almost nothing else meets it, and three
gaps are worth acting on.

**F6 (drifted) — `verify` collapses the eval gate's own taxonomy.**
`gate` distinguishes "this run fabricated" (blocked) from "there is
nothing here to verdict" (undecided, exit 3) from "the evidence is
unreadable" (exit 4). `verify` — the command whose entire job is
deciding whether a run measured anything — exits 1 for all of them
(`evals/cli.py:160-161`, `verify.py:34-83`). An empty run file and a
fabricating run are the same number to CI, in the one place the
platform has already argued they must not be. This is the same
concern, decided in one command and drifted in its sibling.

**F7 (drifted) — a malformed filter gets three different answers in
one process.** `api/app.py:167-174` catches the `ValueError` → HTTP
400. `tools/http.py:20-24` catches nothing → the same error becomes a
500. `analyst/tools.py:100-101` catches it in the generic handler and
labels it `store_unavailable` / `retry` — instructing the model to
retry an unparseable filter forever, when the registry's own table
says invalid input means `rephrase` (`tools/registry.py:41-45`). The
generic handler is load-bearing for D29a's store-death drill
(`evals/faults.py:28`), which is why it swallows everything — so the
fix is a real choice, not a patch.

**F8 (drifted) — the L3 classifier's budget refusal is indistinguish-
able from model uncertainty.** D31 argued this exact distinction for
the gateway. The sentinel's cap produces `tag="unclear"`
(`sentinel/classifier.py:123`) — the same tag a genuinely uncertain
model produces (`classifier.py:94,98`), separated only by a `deferred`
flag. The judge breaker takes a third path (`SystemExit`,
`evals/breaker.py:82`), which D31 explicitly considered and rejected
*for a server* — so that divergence is decided and the sentinel's is
not.

Two smaller observations, recorded without follow-ups. **`SystemExit`
from library modules** is a repo-wide pattern outside the gateway
(`evals/runner.py`, `evals/providers.py`, `sentinel/queue.py`,
`gateway/market.py`): the messages are uniformly excellent, the exit
codes uniformly 1. And **three reconcilers, two conventions**: ingest
and langfuse print `MISMATCH` and exit 1, market prints `DRIFT` and
exits **0** (`gateway/cli.py:74-86`), so the connector's drift
detection has no machine-readable signal — which is why the collector
had to route drift through a step summary.

Also recorded on the decided side, because it is the platform at its
best: budget exhaustion in the analyst harness is *not* an error. It
becomes a conclude-now turn, `degraded=true`, and one of four named
`cutoff_reason` values (`analyst/harness.py:45-53`, D29a) — "an
exception is not a conclusion". The step machine's seven-state
`StepStatus` with its `ROLLBACK_IRREVERSIBLE` sentinel (D28) is the
richest refusal vocabulary in the repo. Both then get flattened to
exit 0/1 at the CLI boundary (`analyst/cli.py:311-313`), which is
where the vocabulary stops.

## Concern 5 — identity: what counts as the same thing?

Every layer has an identity key and they were chosen independently.
Most disagreements are latent — they need a redelivery, a collision,
or a same-second start to surface. Two are not latent.

**F9 (drifted) — `run_id` is a second-granularity timestamp, and four
layers key off it with four different conflict policies.** Both
producers mint it as `strftime("%Y%m%dT%H%M%SZ")`
(`analyst/cli.py:383`, `evals/runner.py:367`). Two runs starting in
the same second are: **an error** to the audit store (PK violation,
`analyst/audit.py:43,86`), **the same rows** to the sink (dropped
silently, `ingest/sink.py:36,67`), **one trace** to Langfuse
(equal derived ids, `langfuse/otlp.py:24`) — which then makes the
round-trip acceptance test report phantom mismatches
(`langfuse/reconcile.py:39-41`) — and **a truncated file** to the
eval runner (`evals/runner.py:369`).

The generalisation is the finding: the same key shape resolves
conflicts four ways across the platform — highest-sequence wins (hot
store, `graph/ingest.py:75`), first write wins (sink,
`sink.py:67`), last write wins (Langfuse upsert), and refuse
(audit store, `audit.py:184`). Amending a recorded event and
re-exporting updates Langfuse, leaves the sink stale, and cannot be
written to the audit store at all. Each choice is right for its
layer; nobody has ever seen them side by side.

**F10 (drifted) — the spool's identity is content, the sink's is
position, and they disagree in both directions.** Batch identity is
`sha256(body)[:16]` (`ingest/spool.py:38`); row identity is
`f"{run_id}:{seq}"` (`worker.py:39`). So the same event in two
differently-composed batches is two raw files and one sink row —
`replay_from_raw` then reports fewer rows written than events read,
silently. And a *changed payload* under the same `(run_id, seq)` is a
brand-new batch to the spool and an already-seen row to the sink,
discarded by `ON CONFLICT DO NOTHING` with no signal;
`ingest/reconcile.py:33-45` compares counts only, so the control
agrees nothing is wrong. D48 decided both keys separately and never
compared them.

**F11 (drifted) — the cold store's stated key is not its implemented
key.** D12 and the module's own docstring say readers dedupe on
`(resource_id, sequence)` (`cold/queries.py:5-7`, `SPEC.md:451`); the
code dedupes on the entire projected row (`queries.py:27,77,122`).
These agree only while duplicates are byte-identical. If a producer
ever re-sends sequence N with different attrs, the hot store keeps the
first (`graph/ingest.py:75`) and cold keeps **both**, with the tie
broken arbitrarily — the two stores answer differently for the same
resource. D12's rejection of an `ingested_at` column exists to keep
duplicates byte-identical, so today's safety comes from that
convention rather than from the key.

**F12 (drifted) — the response cache and the account system disagree
about what a request is.** The cache key is `{alias, model,
max_tokens, messages, system, tools, extra_args}`
(`gateway/cache.py:29`, `server.py:259-272`) — the account is not in
it, while D43 makes the account the identity of a caller.

Stating the consequence precisely, because the alarming version is
wrong: this is **not** a content leak. The key contains the full
request bytes, so a hit requires the caller to have sent byte-
identical messages, system and tools; nobody can pull content they
did not already supply, and at temperature 0 the response is a
deterministic function of that input. What it actually produces is
(i) a **cross-account subsidy** — the hit is recorded at
`cost_usd=0.0` against the second account (`server.py:1145-1152`), so
one account's paid call funds another's free one, in a system whose
whole point is that the meter is trustworthy; and (ii) a narrow
**existence oracle**, since `cached: true` is returned to the caller
(`server.py:1153`), revealing that someone asked this exact question
within the TTL. Both are economic and observational, not
confidential — and neither has been decided.

Two more, recorded without follow-ups because their trigger is
remote: `event_key` is a `:`-concatenation and so is not injective by
construction (`worker.py:39`), and the spool ref is sha256 truncated
to 64 bits, where a collision means a different batch is silently
never persisted (`spool.py:40`).

And one where two layers have already answered a question the schema
records as **open**: `schema.py:8-11` leaves duplicate relationships
undecided, while the hot store merges them idempotently
(`graph/ingest.py:118`) and the cold store preserves the duplicate
inside its relationships JSON (`cold/store.py:109-111`). The layers
did not wait for the decision.

## Findings and follow-ups

| # | Concern | Finding | Follow-up |
|---|---|---|---|
| F1 | memory | the sentinel baseline never ages | #352 |
| F2 | memory | market snapshots have no retention rule | #332 (open) |
| F3 | time | the ingest pipeline captures an observation clock and deletes it | **#349** |
| F4 | time | the audit `ts` means different things to its two readers | #352 |
| F5 | time | two timelines share `events.event_time` | **#348** |
| F6 | refusal | `verify` collapses the taxonomy `gate` argues for | #352 |
| F7 | refusal | a malformed filter gets 400, 500, or `retry` | #352 |
| F8 | refusal | the L3 budget refusal is tagged as model uncertainty | #352 |
| F9 | identity | `run_id` is second-granularity; four layers, four conflict policies | #352 |
| F10 | identity | spool identity is content, sink identity is position | #349 |
| F11 | identity | the cold store's stated key is not its implemented key | #352 |
| F12 | identity | the response cache does not key on the account | **#350** |
| F13 | provenance | the router's provenance requirement is satisfiable by a filename | **#351** |
| F14 | provenance | `baseline.json` gates merges and carries no origin | **#351** |
| F15 | provenance | the sentinel stamps half of what D38 requires | **#351** |
| F16 | provenance | BENCHMARKS.md has no mechanism | #352 |

Four issues carry the findings worth acting on now (#348, #349, #350,
#352); #351 collects the rest as a single backlog rather than
sixteen tickets nobody reads.

## What the review itself taught

**The test is not whether two layers agree.** It is whether the
difference was reasoned about. D41 forgets and D51 does not, and that
is *correct*: serving a request updates the latency window it is
ranked by, and produces no pass^k. The registry admits on an
undeclared capability while the router refuses on an unmeasured one,
and both are argued. Sixteen findings here are differences nobody ever
compared — not one is a layer being obviously wrong.

**The recurring shape is a strict consumer above a lax producer.** The
router refuses a score without provenance; the builder mints that
provenance from a typed path string. The gate defends a baseline; the
baseline command writes it without verifying the run. D38 requires
setup *and* template hash; the classifier stamps the hash. In every
case the guarantee is real and is enforced at the wrong end — at the
only point where checking is impossible, because the value has already
been asserted.

**A published criticism is not a design review.** Days before building
the D48 sink, the phase notes recorded a critique of an external spec
for modelling activities with a single timestamp and no way to express
late arrival. We shipped that shape. Knowing a failure well enough to
write it up is not the same as noticing it in your own diff.

**This review found more than the per-layer reviews that preceded it,
and the reason is structural.** The harnessability review (2026-08)
walked one agent-repository system thoroughly and found four things.
This walked five concerns across every package and found sixteen. Not
because it looked harder — because a question that spans layers cannot
be asked from inside one. Both prior instances of this shape (D41 vs
D44 in mini-phase 13.5, the eval gate vs the router on fabrications in
#328) were found by accident, while doing something else. That is the
argument for making the walk deliberate and periodic.

**What would make it repeatable.** The concern list is the reusable
artifact, not the findings. Memory, time, provenance, refusal and
identity are questions any layered system can be asked, and each one
produced findings here. A sixth is already visible and was not walked:
**authority** — who may cause a side effect, and how does each layer
know. The approval gate, the gateway's scopes, the MCP surface's
curation and the collector's write scope are four answers that have
never been put side by side.
