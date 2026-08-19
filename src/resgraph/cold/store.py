"""Cold-store plumbing — Iceberg catalog, tables, and batch appends (D11/D12).

The whole cold path is in-process: a SQLite-backed catalog and a
filesystem warehouse under one directory (default ./data/cold,
RESGRAPH_COLD_DIR to override).

Decisions: D11, D12 (SPEC.md).
"""

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, TableAlreadyExistsError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform
from pyiceberg.types import LongType, NestedField, StringType, TimestamptzType

from resgraph.schema import UpdateMessage

DEFAULT_DIR = "data/cold"
NAMESPACE = "resgraph"
EVENTS = f"{NAMESPACE}.events"
SNAPSHOTS = f"{NAMESPACE}.state_snapshots"

# attrs/relationships ride as JSON strings: arbitrary attr keys stay
# schema-stable (D12).
EVENTS_SCHEMA = Schema(
    NestedField(1, "sequence", LongType(), required=True),
    NestedField(2, "event_time", TimestamptzType(), required=True),
    NestedField(3, "op", StringType(), required=True),
    NestedField(4, "resource_type", StringType(), required=True),
    NestedField(5, "resource_id", StringType(), required=True),
    NestedField(6, "attrs", StringType(), required=True),
    NestedField(7, "relationships", StringType(), required=True),
    NestedField(8, "schema_version", LongType(), required=True),
)

EVENTS_PARTITION = PartitionSpec(
    PartitionField(source_id=2, field_id=1000, transform=DayTransform(), name="event_day")
)

# watermark = the global max sequence the snapshot reflects (replay
# resumes above it); sequence = the resource's own last-applied event.
SNAPSHOTS_SCHEMA = Schema(
    NestedField(1, "as_of_time", TimestamptzType(), required=True),
    NestedField(2, "watermark", LongType(), required=True),
    NestedField(3, "resource_type", StringType(), required=True),
    NestedField(4, "resource_id", StringType(), required=True),
    NestedField(5, "attrs", StringType(), required=True),
    NestedField(6, "relationships", StringType(), required=True),
    NestedField(7, "sequence", LongType(), required=True),
)

_EVENTS_ARROW = pa.schema(
    [
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("event_time", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("op", pa.string(), nullable=False),
        pa.field("resource_type", pa.string(), nullable=False),
        pa.field("resource_id", pa.string(), nullable=False),
        pa.field("attrs", pa.string(), nullable=False),
        pa.field("relationships", pa.string(), nullable=False),
        pa.field("schema_version", pa.int64(), nullable=False),
    ]
)


def cold_dir() -> Path:
    return Path(os.environ.get("RESGRAPH_COLD_DIR", DEFAULT_DIR))


def get_catalog(directory: Path | None = None) -> SqlCatalog:
    d = directory or cold_dir()
    d.mkdir(parents=True, exist_ok=True)
    return SqlCatalog(
        "resgraph",
        uri=f"sqlite:///{d / 'catalog.db'}",
        warehouse=f"file://{d.resolve()}",
    )


def ensure_tables(catalog: SqlCatalog) -> None:
    """Idempotent — same contract as the hot store's init_schema."""
    with contextlib.suppress(NamespaceAlreadyExistsError):
        catalog.create_namespace(NAMESPACE)
    with contextlib.suppress(TableAlreadyExistsError):
        catalog.create_table(EVENTS, schema=EVENTS_SCHEMA, partition_spec=EVENTS_PARTITION)
    with contextlib.suppress(TableAlreadyExistsError):
        catalog.create_table(SNAPSHOTS, schema=SNAPSHOTS_SCHEMA)


def events_to_arrow(msgs: list[UpdateMessage]) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "sequence": m.sequence,
                "event_time": m.event_time,
                "op": m.op.value,
                "resource_type": m.resource_type.value,
                "resource_id": m.resource_id,
                "attrs": json.dumps(m.attrs, sort_keys=True),
                "relationships": json.dumps(
                    [{"type": r.type, "target_id": r.target_id} for r in m.relationships]
                ),
                "schema_version": m.schema_version,
            }
            for m in msgs
        ],
        schema=_EVENTS_ARROW,
    )


def append_events(catalog: SqlCatalog, msgs: list[UpdateMessage]) -> int:
    """One Iceberg commit per batch — atomic visibility (D11); duplicates
    under at-least-once delivery are the reader's problem by design (D12)."""
    if not msgs:
        return 0
    table = catalog.load_table(EVENTS)
    table.append(events_to_arrow(msgs))
    return len(msgs)


def maintain(catalog: Catalog, directory: Path | None = None) -> dict[str, Any]:
    """Expire non-current Iceberg snapshots on both tables.

    Every append commit is a snapshot; the unexpired snapshot log is
    metadata the planner carries forever. Expiry prunes that log — it
    does NOT reclaim disk (measured: 196 → 1 snapshots, 0 MB freed):
    orphaned files and small-file compaction are engine territory
    (Spark/Trino). The disk numbers in the result state the limitation
    rather than hiding it."""
    from datetime import UTC, datetime

    d = (directory or cold_dir()).resolve()

    def disk() -> float:
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1024**2

    before_mb = disk()
    result: dict[str, Any] = {}
    for name in (EVENTS, SNAPSHOTS):
        table = catalog.load_table(name)
        before = len(table.metadata.snapshots)
        table.maintenance.expire_snapshots().older_than(datetime.now(UTC)).commit()
        table = catalog.load_table(name)
        result[name] = {
            "snapshots_before": before,
            "snapshots_after": len(table.metadata.snapshots),
        }
    result["disk_mb_before"] = round(before_mb, 1)
    result["disk_mb_after"] = round(disk(), 1)
    return result
