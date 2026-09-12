# Model arms: September 2026

## Method

All arms use `evals/scenarios/base.jsonl`: 30 items, three trials per item, the same analyst prompt and tools, and deterministic grading with the narrative judge disabled. The prompt fingerprint is `b041069e`. `pass^k` means all three trials passed; `pass@k` means at least one passed.

The fresh Haiku control scored 0.533 `pass^k`, 15.8% below the published 0.633 run. That met the registered 20% reproduction limit.

## Results

| Source | Run | Model | Reasoning | pass^k | pass@k | Cost | $/passed | p50 | p95 | Fabrications |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| published | `20260813T154547Z` | Claude Haiku 4.5 | none | 0.63 | 0.83 | $1.62 | $0.085 | 20.1s | 28.4s | 2 |
| published | `20260813T200050Z` | Claude Sonnet 4.6 | adaptive | 0.57 | 0.83 | $12.00 | $0.706 | 105.7s | 213.7s | 7 |
| published | `20260813T173553Z` | Claude Opus 4.8 | adaptive | 0.60 | 0.80 | $12.80 | $0.711 | 46.6s | 101.6s | 0 |
| fresh control | `20260912T181205Z` | Claude Haiku 4.5 | none | 0.53 | 0.87 | $1.62 | $0.101 | 16.4s | 25.7s | 3 |
| new | `20260912T193139Z` | Gemini 3.8 Flash | medium | **0.83** | 0.87 | $5.51 | $0.221 | 24.4s | 44.0s | **0** |
| new | `20260912T195315Z` | GPT-5.6 Luna | medium | 0.50 | 0.80 | **$0.32** | **$0.021** | 21.9s | 31.8s | 4 |
| new | `20260912T195314Z` | GPT-5.6 Sol | medium | 0.70 | **0.90** | $3.53 | $0.168 | **18.6s** | 33.7s | **0** |

The dollar figures are worker-only estimates from provider token receipts. Gemini uses Google's introductory rate through 2026-12-31. Sol uses its promotional rate available through at least 2026-11-21.

## New-arm detail

| Model | top-1 | top-3 | Evidence | Honesty | Discipline | Degraded rows |
|---|---:|---:|---:|---:|---:|---:|
| Haiku control | 0.68 | 0.85 | 0.96 | 0.22 | 0.86 | 11 |
| Gemini 3.8 Flash | 0.82 | 0.94 | 1.00 | 0.50 | 0.00 | 43 |
| GPT-5.6 Luna | 0.53 | 0.61 | 0.94 | 0.83 | 0.99 | 2 |
| GPT-5.6 Sol | 0.60 | 0.75 | 1.00 | 1.00 | 1.00 | 6 |

Gemini had the best `pass^k` and no fabrications. It also reached the 15-call limit in 43 rows; 35 of those rows still passed. Its five failed items were three controls, one decoy, and one transitive case. Two rows needed a report-format retry.

Sol was the strongest zero-fabrication OpenAI arm. Its six degraded reports had no harness cutoff. Luna was cheapest, but four rows cited dependency edges that did not exist at incident time; the verification halt fired.

Luna and Sol ran in parallel on separate Memgraph instances. Their latency numbers include that concurrent local load and should not be treated as isolated provider latency measurements.

## Artifacts

| Run | Git ref | SHA-256 |
|---|---|---|
| `20260912T181205Z` | `20650c1` | `49cbca8f7964d20c03f44d2df732c47608d7eca5d1210fa50dd2210820cd35c1` |
| `20260912T193139Z` | `dc32ab0` | `c5ae0b840e848ddb4e7c3603ab6d5701b625c0894db59033eed5d75536caaec5` |
| `20260912T195315Z` | `a008efb` | `b09e2396eab8d70ee11d305f8eb1501b51f4786de4a167e5217cc073b1039f26` |
| `20260912T195314Z` | `a008efb` | `763c51147c6f7d7703293f1867871fd63a8fa395b13b3f03cfce12b8f7e66d2a` |

A Gemini run interrupted by a provider 503 is not included: cached-token accounting changed before it resumed, so its cost and discipline fields were not internally consistent.
