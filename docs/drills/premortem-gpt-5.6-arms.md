# Pre-mortem: GPT-5.6 Luna and Sol arms

## Question

How do GPT-5.6 Luna and Sol perform on the same 30-item, k=3 analyst eval as the fresh Haiku and Gemini runs?

Both arms use medium reasoning, no judge, the same prompt and tools, and the same expected fingerprint (`b041069e`). OpenAI's Responses API is required: Chat Completions rejects function tools when reasoning is enabled for Luna.

## Before the full runs

Each model must pass one real item at k=1. The pilot must:

- record the requested model and expected fingerprint;
- complete more than one tool turn;
- return a valid report with grounded evidence;
- record non-zero token use, latency, and cost.

The Luna pilot is capped at $0.25 and the Sol pilot at $2.00. Full-run caps will be based on the pilot receipts.

## What could invalidate the result

| Risk | Check |
|---|---|
| Responses translation drops reasoning state | Replay every response output item before its function result; covered by a two-turn live probe and provider tests. |
| A different model or task is served | Check model, 30 item IDs, three trials, and one matching fingerprint in every run. |
| A partial run looks complete | Require exactly 90 rows before reporting it. |
| Cached tokens are charged twice | Split `input_tokens_details.cached_tokens` from fresh input before estimating cost. |
| A model answers without using the graph | Count tool-less rows and invalid reports. |
| A provider failure ends a paid run | Resume the same JSONL; do not report partial output. |
| Pricing changes | Record the run date and the published rates used in the table. |

## Decision

Publish all measured quality, cost, latency, cutoff, retry, and fabrication results. A fabrication is a prominent warning, not a reason to hide the arm. Do not compare runs if the item set or fingerprint differs.
