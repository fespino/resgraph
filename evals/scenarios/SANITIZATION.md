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

1. **No secrets.** The item's fields (`description`, `provenance`,
   `tags`) contain no API keys, tokens, or credential-shaped strings.
   Run-trace metadata never migrates wholesale into an item.
2. **No local environment.** No filesystem paths, hostnames,
   usernames, or container names from the machine that produced the
   trace. The only environment facts an item may carry are the
   generator arguments that rebuild its world.
3. **References, not transcripts.** Provenance points at evidence —
   a run id, a scenario id, a sequence number, a failure bucket —
   and never quotes model prose from the trace. Model wording in a
   dataset item leaks the original run's context into every future
   run and can teach the next model the last model's phrasing.
4. **Rebuildable from the recipe.** `rebuild(spec)` must succeed:
   the item is seed + generator args, never a hand-edited world. If
   the failure shape cannot be reproduced by the generator, that is
   a generator gap to file, not a reason to paste a world in by
   hand.
5. **Distinct identity, recorded lineage.** The item's `id` collides
   with nothing in any committed dataset, and its provenance names
   what it derives from (`derived_from`) and what exposed it
   (`exposed_by_run`), so the item's reason to exist is auditable.
6. **Model-agnostic surface.** Nothing in the item names the model
   that failed it or phrases the task around that model's specific
   behavior. Regression items pin a *failure shape*, not a vendor
   quirk; the model that produced the trace is recorded in the run
   file the provenance points to, which is where it belongs.
7. **Synthetic-only content.** Everything in this repo's worlds is
   generator-synthetic. If a trace ever contains non-synthetic
   content (a real incident transcript, external text a model
   quoted), it does not become a dataset item without an explicit
   provenance note and a license/PII review.
