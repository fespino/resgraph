#!/usr/bin/env bash
# INC-001 chaos drill: kill the hot store (with its data) under load,
# watch the freshness alert fire, rebuild from cold history, resume,
# reconcile exact (D17/D18, #54). Scripted so it is reproducible, not
# folklore. Every phase timestamps into a timeline the incident note
# copies verbatim.
set -euo pipefail
cd "$(dirname "$0")/.."

SEED=42 RESOURCES=5000 CHURN=2000000 RATE=8000
COLD_DIR="${RESGRAPH_COLD_DIR:-data/cold-drill}"
TL=/tmp/drill-timeline.txt
PROM=http://localhost:9090

note() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$TL"; }
prom() { curl -sf "$PROM/api/v1/query" --data-urlencode "query=$1" | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "")' || echo ""; }
pending() { docker exec resgraph-redis-1 redis-cli XPENDING resgraph:updates "$1" 2>/dev/null | head -1 || echo "?"; }

rm -f "$TL"; rm -rf "$COLD_DIR"
export RESGRAPH_COLD_DIR="$COLD_DIR"

note "0. stores + obs profile up"
docker compose --profile obs up -d redis memgraph prometheus grafana
sleep 5
docker exec resgraph-redis-1 redis-cli DEL resgraph:updates >/dev/null || true

note "1. cold store init + workers up (hot :9101, cold :9102)"
uv run resgraph cold init
uv run resgraph ingest --metrics-port 9101 --name hot1 >/tmp/drill-hot.log 2>&1 &
HOT_PID=$!
uv run resgraph cold ingest --metrics-port 9102 --name cold1 --batch 8192 >/tmp/drill-cold.log 2>&1 &
COLD_PID=$!
sleep 3

note "2. publisher: snapshot + ${CHURN} churn @ ${RATE}/s (one process — oracle-exact)"
uv run python - "$SEED" "$RESOURCES" "$CHURN" "$RATE" >/tmp/drill-pub.log 2>&1 <<'PY' &
import sys, time
from resgraph.gen.churn import Churn
from resgraph.gen.sinks import RedisSink
from resgraph.gen.world import World
seed, resources, churn_n, rate = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
sink = RedisSink("redis://localhost:6379")
churn = Churn(World(seed, resources))
batch = 500
buf = list(churn.snapshot())
while buf:
    sink.emit_many(buf[:batch]); buf = buf[batch:]
emitted, start = 0, time.monotonic()
while emitted < churn_n:
    n = min(batch, churn_n - emitted)
    sink.emit_many([churn.next_message() for _ in range(n)])
    emitted += n
    ahead = emitted / rate - (time.monotonic() - start)
    if ahead > 0: time.sleep(ahead)
sink.close()
print("published", resources + emitted)
PY
PUB_PID=$!

note "3. steady state: waiting 120s with lag under threshold"
sleep 120
note "   lag=$(prom 'max(ingest_lag)') freshness_ratio_5m=$(prom 'slo:ingest_freshness:ratio_5m')"

note "4. KILL: memgraph down, container removed (its data goes with it)"
T_KILL=$(date -u +%s)
docker compose kill memgraph && docker compose rm -f memgraph

note "5. observing (no intervention): waiting for IngestFreshnessFastBurn to fire"
T_ALERT=""
for _ in $(seq 1 60); do
  sleep 10
  STATE=$(curl -sf "$PROM/api/v1/alerts" | python3 -c 'import json,sys; a=[x for x in json.load(sys.stdin)["data"]["alerts"] if x["labels"]["alertname"]=="IngestFreshnessFastBurn" and x["state"]=="firing"]; print("firing" if a else "")')
  if [ "$STATE" = "firing" ]; then T_ALERT=$(date -u +%s); break; fi
done
if [ -n "$T_ALERT" ]; then
  note "   ALERT FIRED: detection T+$((T_ALERT - T_KILL))s  (lag=$(prom 'max(ingest_lag)') dlq_total=$(prom 'sum(ingest_dlq_total)'))"
else
  note "   ALERT DID NOT FIRE within 10m — drill FAILS its gate"; fi

note "6. recover: stop hot worker FIRST (it would apply its held batch into an empty store before rebuild), fresh memgraph, rebuild from cold"
kill -9 "$HOT_PID" 2>/dev/null || true
docker compose up -d memgraph
sleep 8
T_REBUILD=$(date -u +%s)
uv run resgraph rebuild | tee -a "$TL"
note "   rebuild done in $(( $(date -u +%s) - T_REBUILD ))s; resuming a fresh worker (same group: pending entries replay, watermarks skip them)"
uv run resgraph ingest --metrics-port 9101 --name hot1 >/tmp/drill-hot2.log 2>&1 &
HOT_PID=$!

note "7. drain: waiting for publisher to finish and lag to reach 0"
wait "$PUB_PID" || true
for _ in $(seq 1 180); do
  sleep 5
  LAG=$(prom 'max(ingest_lag)')
  HP=$(pending resgraph-ingest); CP=$(pending resgraph-cold)
  if [ "${LAG%.*}" = "0" ] && [ "$HP" = "0" ] && [ "$CP" = "0" ]; then break; fi
done
T_DRAINED=$(date -u +%s)
note "   drained: lag=$(prom 'max(ingest_lag)')  dlq_total=$(prom 'sum(ingest_dlq_total)') (MUST be 0 or empty — outage is not poison)"

note "8. reconcile against the generator oracle (exact or the drill fails)"
uv run resgraph-gen final-state --seed "$SEED" --resources "$RESOURCES" --count "$CHURN" > /tmp/drill-oracle.jsonl
kill "$HOT_PID" "$COLD_PID" 2>/dev/null || true
sleep 2
if uv run resgraph reconcile --oracle /tmp/drill-oracle.jsonl | tee -a "$TL"; then
  note "   RECONCILE: exact"
else
  note "   RECONCILE: MISMATCH — drill FAILS"; fi

note "9. budget arithmetic (freshness, whole run)"
RATIO=$(prom "avg_over_time(slo:ingest_freshness:good[$(( $(date -u +%s) - T_KILL + 300 ))s])")
note "   good-interval ratio over the run window: ${RATIO} (budget = 1 - 0.99; consumed = (1 - ratio) / 0.01)"
note "done. timeline: $TL  logs: /tmp/drill-*.log  time-to-restored: $((T_DRAINED - T_KILL))s"
