# Detection economics — the funnel and its prices

Methodology: all numbers from `resgraph-sentinel scan`/`classify` on
the committed corpus (381 runs: 361 benign + 20 seeded attacks),
Apple-silicon laptop, 2026-08-18. Layer costs are per-run marginal.

| layer | runs on | cost/run | catches |
|---|---|---|---|
| 1 — rules | 381 (100%) | ~µs, $0 | 20/20 attacks, 0 benign flags |
| 2 — profile | 381 (100% offline; sampled in stream mode) | ~ms, $0 | the volumetric 10/20 + 9 benign novelty flags |
| 3 — classifier | 29 (the funnel) | ~$0.011 | intent tags: injection 4-5/5; benign triage 9/9 |

- **Funnel:** 29/381 runs reach the paid layer (7.6%).
- **Cost per attack surfaced (L1+L2):** $0 — the free layers carry
  the whole recall floor on this corpus.
- **Cost per L3 pass:** ~$0.33 (29 calls + pilot); two registered
  passes total ~$0.66.
- **Cost per correct L3 intent tag:** ~$0.33 / 14 sane verdicts
  (5 attack tags + 9 benign triages) ≈ $0.024.
- The economics conclusion matches the scope D38 narrowed to: the
  paid layer is worth its price for text-borne intent and for
  triaging the ~2.5% benign novelty stream — not for volumetric
  verdicts the free layers already made.

At 1000×: the funnel percentage is the whole game — at this shape,
7.6% of a million runs is 76k paid calls/day (~$800/day), which is
why the flywheel's exclusions and threshold retunes exist: every
false positive removed is paid calls removed, forever.
