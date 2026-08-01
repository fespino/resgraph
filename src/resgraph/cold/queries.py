"""Event-time travel over the cold tables (D13).

state_at(T) = nearest snapshot at or before T, plus a replay of the
events between the snapshot's watermark and T — highest sequence per
resource wins, deletes mean absent. All reads dedupe on
(resource_id, sequence): at-least-once appends make duplicate rows
legal and identical (D12).
"""

import json
from datetime import UTC, datetime

import duckdb
import pyarrow as pa

from .store import EVENTS, SNAPSHOTS

_DATA_COLS = ("resource_type", "attrs", "relationships")


def _state_sql(data_cols: tuple[str, ...]) -> str:
    cols = ", ".join(("resource_id", *data_cols, "sequence"))
    return f"""
WITH delta AS (
    SELECT DISTINCT {cols}, op
    FROM events
    WHERE event_time <= $t AND sequence > $wm
),
unioned AS (
    SELECT {cols}, 'upsert' AS op
    FROM base
    UNION ALL
    SELECT * FROM delta
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY resource_id ORDER BY sequence DESC) AS rn
    FROM unioned
)
SELECT {cols}
FROM ranked
WHERE rn = 1 AND op = 'upsert'
ORDER BY resource_id
"""

_EMPTY_BASE = pa.table(
    {
        "resource_id": pa.array([], pa.string()),
        "resource_type": pa.array([], pa.string()),
        "attrs": pa.array([], pa.string()),
        "relationships": pa.array([], pa.string()),
        "sequence": pa.array([], pa.int64()),
    }
)


def _events_arrow(catalog, min_sequence: int = -1, fields: tuple[str, ...] = ()) -> pa.Table:
    table = catalog.load_table(EVENTS)
    kwargs = {"selected_fields": fields} if fields else {}
    if min_sequence >= 0:
        return table.scan(row_filter=f"sequence > {min_sequence}", **kwargs).to_arrow()
    return table.scan(**kwargs).to_arrow()


def _latest_snapshot(catalog, t: datetime) -> tuple[pa.Table, int]:
    """Rows and watermark of the newest snapshot with as_of_time <= t."""
    snaps = catalog.load_table(SNAPSHOTS).scan().to_arrow()
    if snaps.num_rows == 0:
        return _EMPTY_BASE, -1
    con = duckdb.connect()
    con.register("snaps", snaps)
    row = con.execute(
        "SELECT max(watermark) FROM snaps WHERE as_of_time <= $t", {"t": t}
    ).fetchone()
    if row[0] is None:
        return _EMPTY_BASE, -1
    wm = int(row[0])
    base = con.execute(
        """
        SELECT resource_id, resource_type, attrs, relationships, sequence
        FROM snaps WHERE watermark = $wm
        """,
        {"wm": wm},
    ).arrow()
    return base, wm


def state_at(
    catalog,
    t: datetime,
    use_snapshots: bool = True,
    where: str | None = None,
    params: dict | None = None,
    annotate: bool = False,
    projection: tuple[str, ...] | None = None,
    where_cols: tuple[str, ...] = (),
) -> list[dict]:
    """World state at event time t — one dict per alive resource.

    `where` is a D16-planner-compiled predicate over the state columns.
    annotate=False filters the result; annotate=True keeps every row and
    adds a `matched` bool instead — the composite route needs the full
    topology to traverse, with predicate evaluation still in DuckDB.

    `projection` is the D16 addendum's other half: which data columns to
    return (resource_id and sequence always come back). `where_cols`
    names the columns the predicate reads — scanned and evaluated, never
    returned unless also projected. Both prune at the Iceberg scan, so
    unrequested columns never cross the Arrow boundary.
    """
    out_cols = tuple(c for c in _DATA_COLS if projection is None or c in projection)
    inner_cols = tuple(c for c in _DATA_COLS if c in out_cols or c in where_cols)
    base, wm = _latest_snapshot(catalog, t) if use_snapshots else (_EMPTY_BASE, -1)
    con = duckdb.connect()
    con.register("base", base)
    con.register(
        "events",
        _events_arrow(
            catalog,
            min_sequence=wm,
            fields=("resource_id", "sequence", "op", "event_time", *inner_cols),
        ),
    )
    sql = _state_sql(inner_cols)
    out_sel = ", ".join(("resource_id", *out_cols, "sequence"))
    if where and annotate:
        sql = f"SELECT {out_sel}, ({where}) AS matched FROM ({sql})"
    elif where:
        sql = f"SELECT {out_sel} FROM ({sql}) WHERE {where}"
    elif inner_cols != out_cols:
        sql = f"SELECT {out_sel} FROM ({sql})"
    rows = (
        con.execute(sql, {"t": t, "wm": wm, **(params or {})}).arrow().read_all().to_pylist()
    )
    for r in rows:
        if "attrs" in out_cols:
            r["attrs"] = json.loads(r["attrs"])
        if "relationships" in out_cols:
            r["relationships"] = json.loads(r["relationships"])
        if annotate:
            r["matched"] = bool(r.get("matched")) if where else True
    return rows


def history(catalog, resource_id: str, limit: int = 100) -> list[dict]:
    """A resource's message history, oldest first, deduped."""
    con = duckdb.connect()
    con.register("events", _events_arrow(catalog))
    rows = (
        con.execute(
            """
        SELECT DISTINCT sequence, event_time, op, attrs, relationships
        FROM events WHERE resource_id = $rid
        ORDER BY sequence LIMIT $lim
        """,
            {"rid": resource_id, "lim": limit},
        )
        .arrow()
        .read_all()
        .to_pylist()
    )
    for r in rows:
        r["attrs"] = json.loads(r["attrs"])
        r["relationships"] = json.loads(r["relationships"])
        r["event_time"] = r["event_time"].isoformat()
    return rows


def diff(catalog, t1: datetime, t2: datetime) -> dict:
    """What changed between two moments: created / deleted / changed ids."""
    a = {r["resource_id"]: r for r in state_at(catalog, t1)}
    b = {r["resource_id"]: r for r in state_at(catalog, t2)}
    changed = [
        rid
        for rid in a.keys() & b.keys()
        if (a[rid]["attrs"], a[rid]["relationships"]) != (b[rid]["attrs"], b[rid]["relationships"])
    ]
    return {
        "created": sorted(b.keys() - a.keys()),
        "deleted": sorted(a.keys() - b.keys()),
        "changed": sorted(changed),
    }


def tombstones_at(catalog, t: datetime) -> list[dict]:
    """Resources whose last event at t is a delete — needed by rebuild:
    without them, redelivered pre-t upserts would resurrect the dead.
    Full event scan on purpose (no snapshot base): snapshots hold only
    alive rows, so a delete below the snapshot watermark would hide."""
    con = duckdb.connect()
    con.register("events", _events_arrow(catalog))
    return (
        con.execute(
            """
            SELECT resource_id, resource_type, sequence, event_time
            FROM (
                SELECT DISTINCT resource_id, resource_type, sequence, event_time, op,
                       row_number() OVER (PARTITION BY resource_id ORDER BY sequence DESC) AS rn
                FROM events WHERE event_time <= $t
            )
            WHERE rn = 1 AND op = 'delete'
            ORDER BY resource_id
            """,
            {"t": t},
        )
        .arrow()
        .read_all()
        .to_pylist()
    )


def latest_event_time(catalog) -> datetime | None:
    con = duckdb.connect()
    con.register("events", _events_arrow(catalog))
    return con.execute("SELECT max(event_time) FROM events").fetchone()[0]


def snapshot_at(catalog, t: datetime | None = None) -> dict:
    """Materialize state_at(t) into the snapshots table (t defaults to the
    newest event). The snapshot's watermark is the max sequence at or
    before t across ALL events — deletes included, so replay-from-snapshot
    never re-applies a delete the snapshot already reflects."""
    con = duckdb.connect()
    events = _events_arrow(catalog)
    con.register("events", events)
    if t is None:
        t = con.execute("SELECT max(event_time) FROM events").fetchone()[0]
        if t is None:
            return {"rows": 0, "max_sequence": -1, "as_of_time": None}
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    wm = con.execute(
        "SELECT max(sequence) FROM events WHERE event_time <= $t", {"t": t}
    ).fetchone()[0]
    if wm is None:
        return {"rows": 0, "watermark": -1, "as_of_time": t.isoformat()}
    state = state_at(catalog, t)
    rows = pa.Table.from_pylist(
        [
            {
                "as_of_time": t,
                "watermark": int(wm),
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "attrs": json.dumps(r["attrs"], sort_keys=True),
                "relationships": json.dumps(r["relationships"]),
                "sequence": r["sequence"],
            }
            for r in state
        ],
        schema=pa.schema(
            [
                pa.field("as_of_time", pa.timestamp("us", tz="UTC"), nullable=False),
                pa.field("watermark", pa.int64(), nullable=False),
                pa.field("resource_type", pa.string(), nullable=False),
                pa.field("resource_id", pa.string(), nullable=False),
                pa.field("attrs", pa.string(), nullable=False),
                pa.field("relationships", pa.string(), nullable=False),
                pa.field("sequence", pa.int64(), nullable=False),
            ]
        ),
    )
    catalog.load_table(SNAPSHOTS).append(rows)
    return {"rows": len(state), "watermark": int(wm), "as_of_time": t.isoformat()}
