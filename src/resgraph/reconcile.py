"""Full-state reconciliation (#45): hot store vs cold store vs oracle.

Hot-vs-cold needs no generator knowledge and is the drill's exit
criterion: both stores consumed the same stream, so any disagreement
is a bug in one of them. The optional oracle (a resgraph-gen
final-state dump) additionally catches the failure both stores share —
a message neither ever saw, e.g. evicted from the stream before
either consumer read it.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from neo4j import Session
from pyiceberg.catalog import Catalog

from resgraph.cold.queries import latest_event_time, state_at
from resgraph.graph.client import cypher
from resgraph.graph.ingest import SYSTEM_PROPS


def dump_hot(session: Session) -> dict[str, dict[str, Any]]:
    """Alive, non-phantom state: one dict per resource, edges sorted."""
    rows = cypher(
        session,
        """
        MATCH (n)
        WHERE NOT coalesce(n.deleted, false) AND NOT coalesce(n.phantom, false)
        OPTIONAL MATCH (n)-[r]->(t)
        RETURN properties(n) AS props, labels(n)[0] AS type,
               collect(CASE WHEN r IS NULL THEN NULL
                       ELSE {type: type(r), target: t.id} END) AS rels
        """,
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        props = row["props"]
        out[props["id"]] = {
            "type": row["type"],
            "attrs": {k: v for k, v in props.items() if k not in SYSTEM_PROPS},
            "sequence": props.get("applied_seq"),
            "relationships": sorted(
                (rel["type"].lower(), rel["target"]) for rel in row["rels"] if rel
            ),
        }
    return out


def dump_cold(catalog: Catalog) -> tuple[dict[str, dict[str, Any]], datetime | None]:
    t = latest_event_time(catalog)
    out: dict[str, dict[str, Any]] = {}
    if t is None:
        return out, None
    for r in state_at(catalog, t):
        out[r["resource_id"]] = {
            "type": r["resource_type"],
            "attrs": r["attrs"],
            "sequence": r["sequence"],
            "relationships": sorted((rel["type"], rel["target_id"]) for rel in r["relationships"]),
        }
    return out, t


def load_oracle(path: str | Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out[r["resource_id"]] = {
            "type": r["resource_type"],
            "attrs": r["attrs"],
            "sequence": r["sequence"],
            "relationships": sorted((rel["type"], rel["target_id"]) for rel in r["relationships"]),
        }
    return out


def compare(
    a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]], a_name: str, b_name: str
) -> dict[str, Any]:
    report: dict[str, Any] = {
        f"only_in_{a_name}": sorted(set(a) - set(b)),
        f"only_in_{b_name}": sorted(set(b) - set(a)),
        "attr_mismatches": [],
        "relationship_mismatches": [],
        "sequence_mismatches": [],
    }
    for rid in sorted(set(a) & set(b)):
        if a[rid]["attrs"] != b[rid]["attrs"]:
            report["attr_mismatches"].append(rid)
        if a[rid]["relationships"] != b[rid]["relationships"]:
            report["relationship_mismatches"].append(rid)
        if a[rid]["sequence"] != b[rid]["sequence"]:
            report["sequence_mismatches"].append(rid)
    report["ok"] = not any(v for v in report.values() if isinstance(v, list))  # lists only pre-"ok"
    return report


def reconcile(
    session: Session, catalog: Catalog, oracle: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    hot = dump_hot(session)
    cold, t = dump_cold(catalog)
    result = {
        "as_of": t.isoformat() if t else None,
        "hot_count": len(hot),
        "cold_count": len(cold),
        "hot_vs_cold": compare(hot, cold, "hot", "cold"),
    }
    if oracle is not None:
        result["oracle_count"] = len(oracle)
        result["oracle_vs_cold"] = compare(oracle, cold, "oracle", "cold")
    result["ok"] = all(
        part["ok"] for part in result.values() if isinstance(part, dict) and "ok" in part
    )
    return result
