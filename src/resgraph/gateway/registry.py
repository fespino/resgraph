"""The catalog's structural primitive: one alias, many serving endpoints.

An alias is the request vocabulary (what callers name); an endpoint is
the routable unit (where it runs). A setup MAY declare ``endpoints:`` —
named partial setups merged over the parent — and selection happens among
them; a setup without the key is its own single endpoint, so the 1:1
world is unchanged. Endpoint ids are ``alias@name``; ``@`` is reserved.
"""

from typing import Any

import yaml

CAPABILITIES_KEY = "capabilities"


def expand(setups: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Expand a raw models.yaml mapping into (endpoint table, alias index).

    The endpoint table maps endpoint id -> merged concrete setup; the
    alias index maps alias -> its endpoint ids in declaration order."""
    table: dict[str, dict[str, Any]] = {}
    aliases: dict[str, list[str]] = {}
    for alias, setup in setups.items():
        if "@" in alias:
            raise SystemExit(f"alias {alias!r}: '@' is reserved for endpoint ids")
        entries = setup.get("endpoints")
        if entries is None:
            table[alias] = setup
            aliases[alias] = [alias]
            continue
        if not entries:
            raise SystemExit(f"setup {alias!r}: endpoints must not be empty")
        parent = {k: v for k, v in setup.items() if k != "endpoints"}
        ids: list[str] = []
        for entry in entries:
            name = entry.get("name")
            if not name:
                raise SystemExit(f"setup {alias!r}: every endpoint needs a name")
            eid = f"{alias}@{name}"
            if eid in table:
                raise SystemExit(f"setup {alias!r}: duplicate endpoint name {name!r}")
            table[eid] = {**parent, **{k: v for k, v in entry.items() if k != "name"}}
            ids.append(eid)
        aliases[alias] = ids
    return table, aliases


def capability_mismatch(setup: dict[str, Any], *, wants_tools: bool, max_tokens: int) -> str | None:
    """Why this endpoint cannot serve the request, or None if it can.

    Filters on DECLARED capability only: an undeclared capability admits
    (the catalog is not fully annotated; refusing on ignorance would
    refuse everything). Reversal: flip to strict once every committed
    setup declares its capabilities."""
    caps = setup.get(CAPABILITIES_KEY) or {}
    if wants_tools and caps.get("tools") is False:
        return "request needs tools; endpoint declares tools: false"
    window = setup.get("context_window")
    if window is not None and max_tokens > int(window):
        return f"max_tokens {max_tokens} > declared context_window {window}"
    return None


def load_policy(text: str) -> dict[str, list[str]]:
    """The operator plane: per-caller allow-lists from the policy file.
    Each entry names what a caller MAY reach — aliases, endpoint ids, or
    providers; everything else is refused. A caller with no entry is
    unrestricted (the operator has said nothing, not \"nothing allowed\")."""
    doc = yaml.safe_load(text) or {}
    callers = doc.get("callers") or {}
    out: dict[str, list[str]] = {}
    for name, entry in callers.items():
        allowed = (entry or {}).get("only")
        if not allowed:
            raise SystemExit(f"policy for caller {name!r} needs a non-empty 'only' list")
        out[name] = list(allowed)
    return out


def policy_allows(allowed: list[str], eid: str, setup: dict[str, Any]) -> bool:
    """An endpoint matches an allow-entry by endpoint id, alias, or
    provider — the operator names things at whatever grain they govern."""
    alias = eid.split("@", 1)[0]
    return any(entry in (eid, alias, setup.get("provider")) for entry in allowed)


def endpoint_price(setup: dict[str, Any], prices: dict[str, Any]) -> float | None:
    """One endpoint's effective price per mtok (input + output rates).
    A per-endpoint ``price_per_mtok`` override wins (the same weights can
    price differently per serving location — electricity vs market);
    otherwise the wire model's pricing-table entry; no price on file is
    unmetered, the shared convention."""
    override = setup.get("price_per_mtok")
    if override:
        return float(override.get("input", 0)) + float(override.get("output", 0))
    model = setup.get("model")
    pair = prices.get(model) if model else None
    return (pair[0] + pair[1]) if pair else None


def catalog_rows(
    table: dict[str, dict[str, Any]],
    aliases: dict[str, list[str]],
    routed_aliases: set[str],
    prices: dict[str, Any],
    stats: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The /v1/models rows, from the registry as the single source: the
    alias vocabulary, each endpoint's serving facts, declared capabilities,
    and per-token prices where the pricing table knows the wire model."""
    rows = []
    for alias, ids in sorted(aliases.items()):
        endpoints = []
        for eid in ids:
            setup = table[eid]
            model = setup.get("model")
            price = prices.get(model) if model else None
            endpoints.append(
                {
                    "id": eid,
                    "provider": setup.get("provider", "default"),
                    "model": setup.get("model"),
                    "quant": setup.get("quant"),
                    "context_window": setup.get("context_window"),
                    "pricing": (
                        {"input_per_mtok": price[0], "output_per_mtok": price[1]}
                        if price is not None
                        else None
                    ),
                    # rolling-window percentiles, null until measured — an
                    # unmeasured endpoint is a fact, not a zero
                    "stats": (stats or {}).get(eid),
                }
            )
        rows.append(
            {
                "alias": alias,
                "routed": alias in routed_aliases,
                "capabilities": (table[ids[0]].get(CAPABILITIES_KEY) or {}),
                "endpoints": endpoints,
            }
        )
    return rows
