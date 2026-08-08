#!/usr/bin/env bash
# INC-002 chaos drill: kill the hot store mid-triage and grade what the
# agent does about it (#152). Induced, labeled, reproducible — the same
# posture as the INC-001 drill, pointed at the agent. Design in SPEC.md
# (D29a addendum); pre-registration in EVALS.md.
#
# COSTS MONEY: runs the real suite against the real model. Use a
# project-scoped key with its own cap.
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
note "1. preflight — pre-mortem: docs/drills/premortem-analyst-degraded.md"
note "   chain: fault raises on acquisition -> a tool reading that store fails"
note "          -> the agent sees ok=false -> the report says so -> graded"
if [ "${PILOT:-1}" = "1" ]; then
  note "   pilot: 1 item x 1 trial (~\$0.15) — does the fault reach the agent?"
  head -1 "$SCENARIOS" > /tmp/drill-pilot.jsonl
  PILOT_RUN=$(uv run resgraph-evals run --scenarios /tmp/drill-pilot.jsonl --trials 1 \
    --out-dir /tmp/drill-pilot-runs --no-judge | tail -1)
  if uv run python -c "
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
fired = [c['tool'] for r in rows for c in r.get('tool_trace', []) if not c['ok']]
print('   pilot: fault fired on ' + ', '.join(fired) if fired else '   pilot: FAULT NEVER FIRED')
sys.exit(0 if fired else 1)
" "$PILOT_RUN"; then
    note "   pilot passed — the suite will measure something"
  else
    echo "PREFLIGHT FAILED: the induced fault never reached the agent." >&2
    echo "The suite would complete, produce correct numbers, and measure nothing." >&2
    echo "That is INC-002, twice, for \$5.88. Fix the fault or its target store first." >&2
    echo "Set PILOT=0 to override, and say why in the pre-mortem." >&2
    exit 1
  fi
fi

note "2. running $(wc -l < "$SCENARIOS" | tr -d ' ') degraded items x ${TRIALS} trials"
RUN=$(uv run resgraph-evals run --scenarios "$SCENARIOS" --trials "$TRIALS" | tail -1)
note "3. run file: $RUN"

note "4. the number the drill exists for — found-rate, degraded vs normal"
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

note "5. the trail: every tool error and the strategy shift, agent stopped"
note "   resgraph-analyst audit <run_id> --trace"
note "done — copy this timeline into docs/incidents/INC-002-analyst-degraded.md"
