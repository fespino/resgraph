# EVALS compaction runbook

The working file is model input; the history is the audit record. This
checklist keeps both true when EVALS.md grows past its working set —
compaction is a file operation here, never a judgment call, and never a
summarization. Decision record: SPEC D34.

## The invariants

- **Archive before edit.** A byte-exact snapshot of EVALS.md lands in
  `docs/evals-archive/EVALS-<date>-<gitref>.md`, committed on its own,
  BEFORE any working-file change.
- **Nothing is rewritten, only moved.** Closed material transfers to
  EVALS-HISTORY.md verbatim, appended in original order. No AI
  summarization on this path — a summarized registration is a
  different commitment, and a summary that cannot be diffed cannot be
  audited.
- **What stays in the working file:** the protocol rules, the paid-run
  ledger in full (the base-rate instrument), the environment pin, and
  every OPEN registration verbatim. Open means the registered run has
  not happened and the experiment is live — a parked issue counts as
  live; a closed issue's registration is closed with it.
- **Pointers, both directions.** The working file's history index
  names what moved and where; EVALS-HISTORY.md's head names the
  archive snapshot. "Redo" is one file open, not archaeology.
- **Never fed:** EVALS-HISTORY.md and the archive snapshots are for
  humans and audits; only the working file (its context-core slice)
  reaches a model.

## The procedure

1. `cp EVALS.md docs/evals-archive/EVALS-<date>-$(git rev-parse --short HEAD).md`
   — commit this alone.
2. Map the section boundaries (`grep -n "^## \|^### " EVALS.md`) and
   classify each: working-set (stays) or closed (moves). Check issue
   state for every "run pending" title — titles go stale; the issue is
   the authority.
3. Move with a script that asserts the partition: kept + moved must
   equal the original byte-for-byte, and the verification output goes
   in the PR body with the before/after token counts.
4. Update the working file's history index and EVALS-HISTORY.md's head
   pointer; append, never reorder, in HISTORY.
5. Run the gate; the PR states the fed-context delta.
