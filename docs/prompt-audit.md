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
| tool schemas (TOOL_REGISTRY) | implicit PREFIX | serialized by the API before the breakpoint | editing a tool name/description/schema busts the cache while prompts.py shows no diff. When the cache-hit metric drops with no prompt change, look here first. |
| world summary | SUFFIX | system block 2, after the breakpoint | per-run: counts, alert neighborhood, window bounds |
| alert payload | user message | first user turn | per-run |
| tool results, budget refusals, validation feedback | new messages | appended by the harness | the transcript only grows; nothing edits bytes before the breakpoint (message-order invariants are test-enforced) |

The metric this table protects: token-weighted cache hit rate =
Σ cache_read / Σ input per run (`Usage.cache_hit_rate` in
`src/resgraph/analyst/harness.py`), reported by every eval run, target
≥ 0.9 on multi-turn runs. Token-weighted rather than call-counted so
that one hard miss — a full prefix re-read — is visibly expensive.
