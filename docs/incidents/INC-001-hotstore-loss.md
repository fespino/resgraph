# INC-001: Hot store loss under load (induced)

**Status:** resolved · **Induced:** yes (chaos drill, `scripts/drill-hotstore-loss.sh`, run 2026-08-01)
**Impact:** live traversal API down ~6.5 min; ingest held (not failed) throughout; **zero data loss and zero dead-letters** — the cold store is the system of record, by design and now by evidence.
**Load during incident:** 2,500 updates/s sustained (contended capacity of the co-located stack; the solo-benchmarked 10.5k/s does not survive sharing the machine with the cold consumer's Iceberg commits and the obs stack).

## Timeline (UTC, from the scripted run)

| T | Event |
|---|---|
| 19:29:14 | memgraph killed, container removed — data gone with it (T+0) |
| 19:32:06 | `IngestFreshnessFastBurn` fired — **detection T+172 s** (ratio-cross ~18 s + 2 m `for` + eval cadence; detection via SLO burn, not log-reading) |
| 19:32:34 | recovery begun: worker stopped, fresh memgraph, Bolt-ready |
| 19:32:55 | **rebuild from cold history: 21 s** — 31,641 nodes, 53,880 edges, **19,686 tombstones** (the resurrection guard at drill scale), watermark seq 1,324,500 |
| 19:35:50 | drained: lag 0, `ingest_dlq_total` never materialized — **outage is not poison held under real fire** |
| 19:36:57 | reconcile vs generator oracle: **exact** — 41,196 resources, zero mismatches (ids, attrs, edges, sequences), hot=cold=oracle |

**Time-to-restored: 395 s** (kill → fully drained), most of it unattended.

## Budget arithmetic (D18)

Good-interval ratio over the incident window: 0.633 → the incident consumed the run's entire freshness error budget **~37 times over**. That is what a total-store-loss *should* cost: the budget's job here was detection speed, and any incident of this class exhausts a per-run budget by definition — which is exactly the policy trigger. This note is the policy's required artifact.

## What worked

- Detection via SLO burn — nobody read a log to notice.
- The D14 supersession: the consumer *held* its in-flight batch through a 3.5-minute outage instead of dead-lettering the backlog (the failure mode the pre-supersession code had, found in design review before the drill could find it live).
- Rebuild-under-fire: the phase-4 DR path with the publisher still producing; watermarks restored so the resumed stream replayed nothing.
- Reconcile as exit criterion: "restored" means counts exact against the producer's oracle, not "looks right."

## What didn't (all found by the drill *before* its first successful run)

Three aborted drill runs, each a finding — recorded because an honest drill's failures are its product:

1. Readiness regression: the script `sleep`ed instead of waiting for a Bolt handshake — violating this repo's own phase-3 lesson the first time it applied in a new context.
2. Dirty-store inheritance: the drill initially ran against a store left over from test sessions, whose old watermarks would have silently skipped drill messages. Drills construct their initial state; they never inherit it.
3. A steady-state gate that read a 5-minute SLO ratio 2.5 minutes into the run — judging the startup transient and aborting a healthy system. The window must fit inside the thing it measures.
4. `kill -9` on the process *wrapper* orphaned the worker, which then — correctly, per its outage handling — applied its held batch into the empty store ahead of the rebuild. Killing the wrapper is not killing the worker.

## Action items

- [x] Outage-vs-poison exception classes (D14 supersession) — landed before the drill, verified by it
- [x] Drill script hardened: constructed initial state, handshake waits, in-window gate, pattern kills
- [x] Contended-capacity note in BENCHMARKS: solo numbers carry an implicit "with nothing else running"
