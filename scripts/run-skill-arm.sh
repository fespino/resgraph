#!/usr/bin/env bash
# The paired skill arm: the same 30 scenarios with and without the
# change-forensics playbook in the prefix. Pilot one item each way at
# k=1 first — a fingerprint that does not move means the toggle is wired
# wrong, a $0 discovery.
#
# COSTS MONEY (~$10 at k=3). Project-scoped key with a cap.
set -euo pipefail
cd "$(dirname "$0")/.."

SCENARIOS=evals/scenarios/base.jsonl
JUDGE=claude-opus-4-8
MODEL="${MODEL:-claude-opus-4-8}"
TRIALS="${TRIALS:-3}"
OUT=evals/runs/skill

docker compose up -d >/dev/null
uv run python -c "
from resgraph.graph.client import get_driver
d = get_driver(); d.verify_connectivity(); d.close()
"

if [ "${PILOT:-1}" = "1" ]; then
  head -1 "$SCENARIOS" > /tmp/skill-pilot.jsonl
  wr=$(uv run resgraph-evals run --scenarios /tmp/skill-pilot.jsonl --trials 1 \
    --model "$MODEL" --judge-model "$JUDGE" --out-dir /tmp/skill-pilot-runs | tail -1)
  wor=$(uv run resgraph-evals run --scenarios /tmp/skill-pilot.jsonl --trials 1 \
    --model "$MODEL" --judge-model "$JUDGE" --no-skill --out-dir /tmp/skill-pilot-runs | tail -1)
  uv run python - "$wr" "$wor" <<'PY'
import json, sys
fp = lambda p: {json.loads(l)["cache_fingerprint"] for l in open(p) if l.strip()}
if fp(sys.argv[1]) == fp(sys.argv[2]):
    print("PILOT FAILED: the skill toggle did not move the fingerprint.")
    sys.exit(1)
print("pilot: fingerprints differ — the toggle is wired")
PY
fi

wr=$(uv run resgraph-evals run --scenarios "$SCENARIOS" --trials "$TRIALS" \
  --model "$MODEL" --judge-model "$JUDGE" --out-dir "$OUT" | tail -1)
echo "with-skill: $wr"
wor=$(uv run resgraph-evals run --scenarios "$SCENARIOS" --trials "$TRIALS" \
  --model "$MODEL" --judge-model "$JUDGE" --no-skill --out-dir "$OUT" | tail -1)
echo "without-skill: $wor"

echo "--- what the playbook bought ---"
uv run resgraph-evals skill-value "$wr" "$wor"
