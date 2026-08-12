#!/usr/bin/env bash
# Model arms: the same 30 scenarios under three worker models,
# judge pinned on Opus. Pilot one item per arm at k=1 first — a wrong
# model id or a pricing gap is a $0 discovery, not a $3 one.
#
# COSTS MONEY (~$8.50 for the full run). Project-scoped key with a cap.
set -euo pipefail
cd "$(dirname "$0")/.."

SCENARIOS=evals/scenarios/base.jsonl
JUDGE=claude-opus-4-8
TRIALS="${TRIALS:-3}"
OUT=evals/runs/arms
ARMS=("opus=claude-opus-4-8" "sonnet=claude-sonnet-4-6" "haiku=claude-haiku-4-5")

docker compose up -d >/dev/null
uv run python -c "
from resgraph.graph.client import get_driver
d = get_driver(); d.verify_connectivity(); d.close()
"

if [ "${PILOT:-1}" = "1" ]; then
  head -1 "$SCENARIOS" > /tmp/arm-pilot.jsonl
  for arm in "${ARMS[@]}"; do
    model="${arm#*=}"
    echo "pilot: $arm (1 item, k=1)"
    uv run resgraph-evals run --scenarios /tmp/arm-pilot.jsonl --trials 1 \
      --model "$model" --judge-model "$JUDGE" --out-dir /tmp/arm-pilot-runs >/dev/null
  done
  echo "pilots passed — every arm's worker id and pricing resolve"
fi

declare -a labels
for arm in "${ARMS[@]}"; do
  label="${arm%%=*}"; model="${arm#*=}"
  echo "arm: $label ($model) x ${TRIALS}"
  run=$(uv run resgraph-evals run --scenarios "$SCENARIOS" --trials "$TRIALS" \
    --model "$model" --judge-model "$JUDGE" --out-dir "$OUT" | tail -1)
  labels+=("$label=$run")
done

echo "--- cost per passed triage, by tier ---"
uv run resgraph-evals arms "${labels[@]}" --baseline opus
