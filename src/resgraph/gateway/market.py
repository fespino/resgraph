"""The market connector: ingest OpenRouter's public models catalog.

The one place this codebase consumes the reference gateway instead of
replicating it. Deliberately small: one pull, committed snapshots,
and exactly one consumer (the market-baseline price comparison).

The endpoint answers without auth today but the OpenAPI spec marks it
bearer-authenticated, so 401/403 is a defined terminal outcome — the
open door closed; stop polling — never an error to retry. Snapshots
keep the full schema shape for the drift tests but redact the one
authored field (`description`): facts are free, prose is someone's.

Decisions: D46 (SPEC.md).
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx

MODELS_URL = "https://openrouter.ai/api/v1/models"
USER_AGENT = "resgraph-catalog-connector/0.1 (+https://github.com/fespino/resgraph)"
SNAPSHOT_DIR = Path("evals/market")
REDACTED = "[redacted: authored prose is not republished; see the source url]"
REQUIRED_ROW_FIELDS = ("id", "name", "context_length", "pricing")


def fetch(
    url: str = MODELS_URL, transport: httpx.BaseTransport | None = None
) -> list[dict[str, Any]]:
    """One polite pull of the catalog, shape-validated, always under a
    User-Agent naming this repo. The pre-flight cadence is at most
    daily; the endpoint is CDN-cached for 300s, so anything faster
    only re-reads Cloudflare anyway."""
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30, transport=transport
    ) as client:
        resp = client.get(url)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"market catalog now requires auth (HTTP {resp.status_code}): the open "
            "access was observed behavior, not contract — stop polling; this is a "
            "defined outcome, not an error to retry"
        )
    if resp.status_code == 429:
        raise SystemExit("market catalog rate-limited us (HTTP 429): stop for this run")
    if resp.status_code != 200:
        raise SystemExit(f"market catalog answered HTTP {resp.status_code}; refusing to ingest")
    return validate(resp.json())


def validate(doc: Any) -> list[dict[str, Any]]:
    """The drift gate, applied to the wire response and to every loaded
    snapshot alike: name the problem and refuse, never best-effort."""
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SystemExit("market catalog drifted: expected a non-empty 'data' list")
    for i, row in enumerate(rows):
        missing = [k for k in REQUIRED_ROW_FIELDS if not (isinstance(row, dict) and row.get(k))]
        if missing:
            raise SystemExit(
                f"market catalog drifted: row {i} ({row.get('id', '?')}) lacks {missing}"
            )
        try:
            float(row["pricing"]["prompt"])
            float(row["pricing"]["completion"])
        except (KeyError, TypeError, ValueError):
            raise SystemExit(
                f"market catalog drifted: row {row['id']} pricing is not "
                "float-parseable prompt/completion"
            ) from None
    return rows


def redact(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Full shape, no prose: every key survives so the drift tests see
    the whole schema, but authored text is replaced in place."""
    return [
        {k: (REDACTED if k == "description" and v else v) for k, v in row.items()} for row in rows
    ]


def snapshot(rows: list[dict[str, Any]], *, path: Path, url: str, fetched_at: str) -> Path:
    doc = {
        "source": url,
        "fetched_at": fetched_at,
        "model_count": len(rows),
        "data": redact(rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n")
    return path


def load_snapshot(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text())
    if not doc.get("source") or not doc.get("fetched_at"):
        raise SystemExit(f"snapshot {path} lacks source/fetched_at: no provenance, no baseline")
    validate(doc)
    return doc


def field_sets(rows: list[dict[str, Any]]) -> dict[frozenset[str], int]:
    """The distinct row shapes present, with how many rows carry each.
    Catalog rows legitimately differ (an omitted optional field is not
    drift), so the shapes are a fingerprint to compare ACROSS pulls —
    never a count to threshold within one."""
    shapes: dict[frozenset[str], int] = {}
    for row in rows:
        key = frozenset(row)
        shapes[key] = shapes.get(key, 0) + 1
    return shapes


def drift(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[str]:
    """What changed in the catalog's SHAPE between two pulls. Names the
    fields rather than requiring anyone to have enumerated them: a
    field nobody declared is exactly the one that gets missed."""
    before = {field for shape in field_sets(previous) for field in shape}
    after = {field for shape in field_sets(current) for field in shape}
    findings = []
    if appeared := sorted(after - before):
        findings.append(f"fields new since the previous pull: {appeared}")
    if vanished := sorted(before - after):
        findings.append(f"fields gone since the previous pull: {vanished}")
    shapes_before, shapes_after = len(field_sets(previous)), len(field_sets(current))
    if shapes_before != shapes_after:
        findings.append(f"distinct row shapes: {shapes_before} -> {shapes_after}")
    return findings


def _normalize(name: str) -> str:
    return re.sub(r"[.:]", "-", name.lower())


def market_prices(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalized id-tail -> the listing's per-mtok facts. A tail two
    authors share auto-matches nothing (a wrong price silently
    attributed is worse than an honest 'unmatched'); an explicit
    `market:` id in models.yaml still reaches those rows exactly."""
    by_tail: dict[str, dict[str, Any] | None] = {}
    for row in rows:
        tail = _normalize(row["id"].split("/", 1)[-1])
        by_tail[tail] = None if tail in by_tail else _listing(row)
    return {t: v for t, v in by_tail.items() if v is not None}


def _listing(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "per_mtok": (float(row["pricing"]["prompt"]) + float(row["pricing"]["completion"]))
        * 1_000_000,
        "context_length": row["context_length"],
    }


def baseline(
    table: dict[str, dict[str, Any]],
    prices: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The one consumer: per endpoint, our effective price next to the
    market's listing for the same weights. `ours` None is the free
    tier (local serving is unmetered); `market` None is unmatched."""
    from resgraph.gateway.registry import endpoint_price

    tails = market_prices(rows)
    exact = {row["id"]: _listing(row) for row in rows}
    out = []
    for eid, setup in sorted(table.items()):
        declared = setup.get("market")
        listing = exact.get(declared) if declared else tails.get(_normalize(setup.get("model", "")))
        ours = endpoint_price(setup, prices)
        out.append(
            {
                "endpoint": eid,
                "ours_per_mtok": ours,
                "market_id": listing["id"] if listing else None,
                "market_per_mtok": listing["per_mtok"] if listing else None,
                "ratio": (
                    round(ours / listing["per_mtok"], 3)
                    if ours and listing and listing["per_mtok"]
                    else None
                ),
            }
        )
    return out
