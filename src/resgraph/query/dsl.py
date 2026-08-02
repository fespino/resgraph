"""Filter DSL: conjunctions of comparisons, nothing else (D16).

`type=vm AND attrs.zone=z1 AND attrs.cpu>=4` parses into Predicates;
OR, grouping, and functions are rejected at the boundary. Parse, don't
validate: downstream code only ever sees well-formed Predicates.
"""

import re
from dataclasses import dataclass
from typing import Literal, cast

Op = Literal["=", "!=", "<", "<=", ">", ">="]

_TERM = re.compile(
    r"^(?P<field>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<op><=|>=|!=|=|<|>)\s*(?P<value>\S+)$"
)
_AND = re.compile(r"\bAND\b", re.IGNORECASE)

MAX_FILTER_LEN = 512


@dataclass(frozen=True)
class Predicate:
    field: str
    op: Op
    value: str | int | float | bool

    def __str__(self) -> str:
        return f"{self.field}{self.op}{self.value}"


def _coerce(raw: str) -> str | int | float | bool:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_filter(text: str | None) -> list[Predicate]:
    """Parse a filter string into predicates; raise ValueError on anything
    outside the D16 grammar."""
    if not text or not text.strip():
        return []
    if len(text) > MAX_FILTER_LEN:
        raise ValueError(f"filter longer than {MAX_FILTER_LEN} chars")
    if re.search(r"\bOR\b", text, re.IGNORECASE):
        raise ValueError("OR is not supported (D16: conjunctions only)")
    if "(" in text or ")" in text:
        raise ValueError("grouping is not supported (D16: conjunctions only)")
    preds = []
    for term in (t.strip() for t in _AND.split(text.strip())):
        m = _TERM.match(term)
        if not m:
            raise ValueError(f"cannot parse filter term: {term!r}")
        value = _coerce(m["value"])
        op = cast(Op, m["op"])  # _TERM's op group closes exactly this set
        if op in ("<", "<=", ">", ">=") and isinstance(value, str):
            raise ValueError(f"ordering comparison needs a numeric value: {term!r}")
        preds.append(Predicate(m["field"], op, value))
    return preds
