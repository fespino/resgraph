#!/usr/bin/env bash
# Pre-refresh pilot (#180): one coverage-gap item, k=1, ~$0.15. Checks
# the deferral field's one job — can the real model express the planted
# gap — BEFORE the ~$13.50 refresh certifies the schema (#153/#179).
#
# COSTS MONEY: one real run. Use a project-scoped key with its own cap.
set -euo pipefail
cd "$(dirname "$0")/.."

ITEM=evals/scenarios/gap-pilot.jsonl

docker compose up -d >/dev/null
uv run python -c "
from resgraph.graph.client import get_driver
d = get_driver(); d.verify_connectivity(); d.close()
"
RUN=$(uv run resgraph-evals run --scenarios "$ITEM" --trials 1 \
  --out-dir /tmp/gap-pilot-runs --no-judge | tail -1)
echo "run file: $RUN"

uv run python - "$RUN" "$ITEM" <<'PY'
import json, sys
from datetime import datetime

row = json.loads(open(sys.argv[1]).read().splitlines()[-1])
spec = json.loads(open(sys.argv[2]).read())
report = row.get("report") or {}
deferral = report.get("deferral")
cut = datetime.fromisoformat(spec["provenance"]["gap_before"])
print("suspects:", len(report.get("suspects", [])),
      "| no_confident_candidate:", report.get("no_confident_candidate"))
print("deferral:", json.dumps(deferral, indent=2) if deferral else None)
print("evidence dim:", row["dims"].get("evidence"))
if deferral is None:
    print("PILOT FAILED: no deferral on a world whose deciding evidence is withheld.")
    print("Revise the schema or the prompt rule BEFORE the baseline refresh —")
    print("certifying a field the model cannot use costs a second refresh.")
    sys.exit(1)
start = datetime.fromisoformat(deferral["window_start"])
end = datetime.fromisoformat(deferral["window_end"])
overlaps = start < cut and end > datetime.fromisoformat("2026-01-01T00:00:00+00:00")
print(f"store={deferral['store']} window_overlaps_planted_gap={overlaps}")
if deferral["store"] != "cold" or not overlaps:
    print("PILOT SOFT-FAIL: deferred, but not at the planted gap — read the narrative")
    print("before deciding whether the schema or the item needs revision.")
    sys.exit(2)
print("PILOT PASSED: the field is expressible — the refresh may proceed.")
PY
