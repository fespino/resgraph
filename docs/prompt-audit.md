# Prompt cache audit — resgraph-analyst

Every section of the analyst's prompt carries a verdict here: PREFIX
(static, cached) or SUFFIX (runtime). The rule (D23): a section goes to
SUFFIX only if it genuinely differs on every run — nothing gets hedged
out of the cache "because it might change." This table and
`src/resgraph/analyst/prompts.py` change together; a test holds the
section list in sync.

| Section | Verdict | Position | Why |
|---|---|---|---|
| identity | PREFIX | system block 1 | never varies |
| triage discipline (change-forensics skill body) | PREFIX | system block 1 | the committed playbook; changes only when the skill file does |
| tool guidance | PREFIX | system block 1 | steering conventions, static — budget *numbers* stay out of the prompt entirely (D22: budgets live in the harness) |
| output contract (TriageReport schema + rules) | PREFIX | system block 1, last section; the block carries the `cache_control` breakpoint | derived from `models.py` — changing the report contract is *supposed* to bust the cache, visibly |
| tool schemas (TOOL_REGISTRY) | implicit PREFIX | serialized by the API before the breakpoint | editing a tool name/description/schema busts the cache while prompts.py shows no diff. Measured, not just documented: `cache_fingerprint` hashes tool blocks + prefix into every run record, so a cache-hit drop diffs to either "fingerprint changed — find the registry/prompt edit" or "fingerprint stable — runtime bug". |
| world summary | SUFFIX | system block 2, after the breakpoint | per-run: counts, alert neighborhood, window bounds |
| alert payload | user message | first user turn | per-run |
| tool results, budget refusals, validation feedback | new messages | appended by the harness | the transcript only grows; nothing edits bytes before the breakpoint (message-order invariants are test-enforced) |

The metric this table protects: token-weighted cache hit rate =
Σ cache_read / Σ input per run (`Usage.cache_hit_rate` in
`src/resgraph/analyst/harness.py`), reported by every eval run, target
≥ 0.9 on multi-turn runs. Token-weighted rather than call-counted so
that one hard miss — a full prefix re-read — is visibly expensive.

Diagnosing a rate drop, three branches (not two):

1. **Fingerprint changed** — a registry or prompt edit busted the
   cache; find the diff.
2. **Fingerprint stable, prefix above the model's minimum** — runtime
   behavior (retry editing history, message-order violation).
3. **Fingerprint stable, prefix below the model's minimum** — the API
   caches nothing below a per-model floor (1,024 tokens for Opus 4.8;
   512–4,096 across families) and returns **no error**. A shrunken
   prefix stops caching silently. Check estimated prefix tokens before
   suspecting the harness.

Two more API facts the eval runner leans on (prompt-caching docs):
a cache entry only becomes usable after the first response *begins*,
so parallel trials sharing a cold prefix each pay the write — the
runner is serial per scenario on purpose; and `usage.input_tokens`
counts only tokens after the last breakpoint, which is why
`Usage.total_input` sums all three fields before dividing.
