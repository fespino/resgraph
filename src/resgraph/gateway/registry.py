"""The catalog's structural primitive: one alias, many serving endpoints.

An alias is the request vocabulary (what callers name); an endpoint is
the routable unit (where it runs). A setup MAY declare ``endpoints:`` —
named partial setups merged over the parent — and selection happens among
them; a setup without the key is its own single endpoint, so the 1:1
world is unchanged. Endpoint ids are ``alias@name``; ``@`` is reserved.
"""

from typing import Any

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


def catalog_rows(
    table: dict[str, dict[str, Any]],
    aliases: dict[str, list[str]],
    routed_aliases: set[str],
    prices: dict[str, Any],
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
