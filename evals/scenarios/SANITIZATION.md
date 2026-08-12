# Eval-item sanitization checklist

Run this checklist before any dataset item is mined from a real run
trace (D24, the eval contract: iteration failures become permanent
regression items). A planted item is born clean — the generator
invents every name and value. A mined item starts life inside a
recorded trace that also contains prompts, tool results, model prose,
and the runner's environment, and this checklist is the boundary
between the two.

Every item in a `*.jsonl` dataset here must pass all seven checks.
The mining PR links the checklist and states that it ran.

Each check is marked with its owner. **Enforced** means a validator
in `resgraph/evals/sanitize.py` (#126) decides it: the mining script
refuses a failing item before writing it, and the CI sweep
(`tests/test_dataset_sanitization.py`) re-checks every committed
dataset, so an item hand-edited past the miner is caught too.
**Review** means the check has no computable arbiter and a human
answers it at PR time — a validator there would fake a precision it
does not have.

1. **No secrets.** *Enforced* (`secrets` validator; findings report
   spans, never the match). The item's fields (`description`,
   `provenance`, `tags`) contain no API keys, tokens, or
   credential-shaped strings. Run-trace metadata never migrates
   wholesale into an item.
2. **No local environment.** *Enforced* (`local_env` validator). No
   filesystem paths, hostnames, usernames, or container names from
   the machine that produced the trace. The only environment facts
   an item may carry are the generator arguments that rebuild its
   world.
3. **References, not transcripts.** *Review.* Provenance points at
   evidence — a run id, a scenario id, a sequence number, a failure
   bucket — and never quotes model prose from the trace. Model
   wording in a dataset item leaks the original run's context into
   every future run and can teach the next model the last model's
   phrasing. Whether a note leaks context is a judgment call.
4. **Rebuildable from the recipe.** *Enforced* (the miner calls
   `rebuild()` on every item; `tests/test_regression_dataset.py`
   re-checks the committed file). The item is seed + generator args,
   never a hand-edited world. If the failure shape cannot be
   reproduced by the generator, that is a generator gap to file, not
   a reason to paste a world in by hand.
5. **Distinct identity, recorded lineage.** *Enforced* (the miner
   refuses id collisions; the `lineage` validator requires
   `derived_from`, `exposed_by_run`, and `bucket` on every
   failure-derived item). The item's reason to exist is auditable.
6. **Model-agnostic surface.** *Enforced for names* (`model_names`
   validator), *review for phrasing*: no scanner can decide whether
   a description is written around one model's specific behavior.
   Regression items pin a *failure shape*, not a vendor quirk; the
   model that produced the trace is recorded in the run file the
   provenance points to, which is where it belongs.
7. **Synthetic-only content.** *Review.* Everything in this repo's
   worlds is generator-synthetic. If a trace ever contains
   non-synthetic content (a real incident transcript, external text
   a model quoted), it does not become a dataset item without an
   explicit provenance note and a license/PII review.
8. **Injection content is sentinel-marked and template-fixed.**
   *Enforced* (`injection_findings` validator, #160). Injection items
   deliberately carry adversarial text, which would otherwise be a
   place for un-swept content to hide behind "that's the injection
   item". The boundary: only an `injection`-tagged item may carry the
   `[[SYNTHETIC-INJECTION]]` sentinel, its `inject_text` must equal the
   canonical template computed from its declared `inject_target`
   (nothing arbitrary can live in the field), and the sentinel
   appearing anywhere on a non-injection item is a finding.
