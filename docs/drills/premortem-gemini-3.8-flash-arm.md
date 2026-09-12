# Pre-mortem: Gemini 3.8 Flash arm

## Question

How does Gemini 3.8 Flash perform on the same 30-item, k=3 analyst eval as the fresh Haiku run?

The arm uses Google's OpenAI-compatible endpoint, medium reasoning, no judge, and introductory pricing valid through 2026-12-31. The full run must match the Haiku item set and prompt fingerprint (`b041069e`).

## Measurement path

1. `evals/models.yaml` pins the model, endpoint, credential variable, and reasoning effort.
2. `providers.py` translates the fixed prompt and tools to Chat Completions and replays provider metadata between tool turns.
3. `runner.py` reloads the planted world for each trial and records the model, prompt fingerprint, trace, tokens, latency, and grades.
4. `arms.py` compares runs only when their item sets match.

## Pilot

The pilot used `ambiguous-s42006`, one trial, no judge, with a $0.50 cap.

The first attempt failed on the second model turn. Gemini returned an encrypted thought signature with its tool call; the adapter dropped it, so Google rejected the next request. The adapter now preserves and replays `extra_content.google.thought_signature`, with a two-turn provider test.

The repeated pilot passed: 12 tool calls, a valid report on the first submission, the planted cause ranked first, grounded evidence, and no fabrications. It used 78,512 input tokens and 1,122 output tokens, took 29.536 seconds, and cost $0.0631. Ninety rows at that rate project to $5.68; the full run is capped at $15 and each item at $0.50.

The first full run hit a provider 503 after 35 rows. Cached-token accounting was added before it resumed, so its 90 rows used two accounting paths. That file is excluded. The publishable run starts from row zero with the tested adapter unchanged.

## What could invalidate the result

| Risk | Check |
|---|---|
| Wrong model or prompt | Require one model ID and the expected fingerprint in every row. |
| Broken tool translation | Count tool-less rows, invalid reports, and validation retries. |
| Partial run | Require 90 rows, 30 item IDs, and trials 0, 1, and 2. |
| Replayed responses | Check distinct token and latency receipts across trials. |
| Missing cost data | Require positive usage and cost; record the dated price. |
| Provider outage | Resume the same JSONL; do not report partial output. |

The compatible endpoint reports cache reads as OpenAI-style `cached_tokens`, not Anthropic cache fields. The adapter maps those receipts into the common token counters before grading discipline and estimating cost.

## Decision

Publish pass rates, slices, cost, latency, cutoffs, retries, and every fabrication against the fresh Haiku arm. Do not compare the runs if the item set or fingerprint differs.
