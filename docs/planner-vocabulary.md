# The mini planner, in query-engine vocabulary

The D16 planner is ~200 lines, but every move it makes has an industry
name. This doc maps each one to the term you'd meet in Trino,
DataFusion, or Spark — partly so the code reads as a deliberate small
version of a real thing, partly because the names are how you find the
literature. Andy Grove's
[*How Query Engines Work*](https://howqueryengineswork.com/) covers
the full-size versions of everything below.

| This repo | The industry name | Where the big engines do it |
|---|---|---|
| a predicate compiled into Cypher/DuckDB `WHERE` instead of filtered in Python | **predicate push-down** | scan operators in every engine; DataFusion's `filter_pushdown`, Iceberg row-filter scans |
| `state_at(projection=..., where_cols=...)` pruning columns at the Iceberg scan | **projection push-down** | the book's data-source contract is `scan(projection)` — columns first, predicates second; its optimizer's first rule. We built this the wrong way round and the book review caught it (D16 addendum): composite p50 0.371 → 0.250 s at 1M events |
| the residual step, flagged in the plan | **post-scan filter / residual predicate** | what an engine does with predicates a source can't evaluate — the difference is that ours is loudly visible, and measured (2.5× at 1M events, BENCHMARKS.md) |
| `plan()` returns data; nothing runs until `.execute()` | **lazy evaluation / logical plan** | DataFrame chains that defer until `collect()`; logical→physical plan separation |
| `?explain=true` returning the plan without executing | **EXPLAIN** | `EXPLAIN` in every SQL engine; ours can't show row estimates, which is the next row's point |
| the `place()` table lookup (field → capable stores) | **capability-based placement**, the degenerate case of a **cost-based optimizer** | real engines choose between *multiple possible* placements using statistics (row counts, selectivity); we refuse to have that problem — the moment two placements are both possible, D16's reversal condition fires |
| one query spanning Memgraph + Iceberg/DuckDB | **federated query** | Trino's connectors; the reversal condition names Trino as the adopt-line for a third store |
| the JSON `explain` output | **serialized plan**; the cross-engine standard is **Substrait** | plans-as-data enables interchange between engines; ours stops at JSON because exactly one engine consumes it |
| keeping filter + projection inside DuckDB/Arrow until the last step | **zero-copy / staying behind the Arrow boundary** | Arrow's whole pitch is sharing memory without reformatting; the measured residual tax below is what leaving the boundary costs |
| composite = reconstruct in cold, traverse in memory | **materialized intermediate / broadcast to a local operator** | shipping a filtered scan to a specialized operator; the benchmark shows the scan is 94–97% of the cost, the operator is noise |

Two findings from measuring rather than reading:

- **Push-down is about the boundary, not the comparison.** The residual
  path loses because every row must cross Arrow → Python → JSON-parse
  before the filter sees it, and that tax scales with the world while
  the pushed filter scales with matches. This is why engines fight to
  keep work behind the Arrow boundary, and why "just filter it in the
  app" gets slower the better your data layer gets.
- **A visible residual is a design instrument.** Real optimizers hide
  post-filters in plan leaves; ours promotes them to a top-level plan
  field precisely so a slow query explains itself — if `residual` is
  non-empty, you're paying the boundary tax, and the fix (add the field
  to the placement table, or stop asking) is legible in the plan.
