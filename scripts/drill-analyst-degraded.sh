#!/usr/bin/env bash
# INC-002 chaos drill: kill the hot store mid-triage and grade what the
# agent does about it (#152). Induced, labeled, reproducible — the same
# posture as the INC-001 drill, pointed at the agent instead of the
# ingest.
#
# The fault is injected at the store handle, not faked in the prompt:
# after DEGRADED_KILL_AFTER tool calls the hot session factory raises,
# so blast_radius/dependency_path/fetch_resource fail through their own
# error path while resource_history/world_diff keep reading cold. A
# well-harnessed agent finishes with history-only triage and says so.
#
# COSTS MONEY: this runs the real suite against the real model. Read
# EVALS.md's protocol rules before running, and use a project-scoped key
# with its own cap.
set -euo pipefail
cd "$(dirname "$0")/.."

TRIALS="${TRIALS:-3}"
SCENARIOS=evals/scenarios/degraded.jsonl
BASELINE=evals/baseline.json
TL=/tmp/drill-analyst-degraded.txt

note() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$TL"; }

rm -f "$TL"
note "0. stores up; the drill kills the hot store IN-PROCESS, so the container stays healthy"
docker compose up -d >/dev/null
uv run python -c "
from resgraph.graph.client import get_driver
d = get_driver(); d.verify_connectivity(); d.close()
"
note "1. running $(wc -l < "$SCENARIOS" | tr -d ' ') degraded items x ${TRIALS} trials"
RUN=$(uv run resgraph-evals run --scenarios "$SCENARIOS" --trials "$TRIALS" | tail -1)
note "2. run file: $RUN"

note "3. the number the drill exists for — found-rate, degraded vs normal"
uv run python - "$RUN" "$BASELINE" <<'PY' | tee -a "$TL"
import json, sys
from resgraph.evals.report import aggregate

rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
run = aggregate(rows)
base = json.load(open(sys.argv[2]))
found_degraded = run["dims"].get("found_top3")
found_normal = base["dims"].get("found_top3")
print(f"  degraded honesty (pass^k on the degraded dim): {run['pass_all_trials']}")
print(f"  found_top3 degraded: {found_degraded}")
print(f"  found_top3 normal (certified baseline): {found_normal}")
if found_degraded is not None and found_normal is not None:
    print(f"  cost of honest degradation: {found_degraded - found_normal:+.3f}")
print(f"  fabrications after the kill: {run['fabrication_count']}")
PY

note "4. the trail: every tool error and the strategy shift, agent stopped"
note "   resgraph-analyst audit <run_id> --trace"
note "done — copy this timeline into docs/incidents/INC-002-analyst-degraded.md"
