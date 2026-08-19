"""The in-line screening seat: the sentinel's injection signatures
pointed at requests before they are served — the same measured rules,
a second seat.

Screening OBSERVES, it never blocks: this platform's own traffic
carries adversarial text as data by design (the analyst reads planted
alert text; the injection evals depend on it arriving intact), so an
in-line block would break the workload the gateway exists to serve. A
match is a flag on the audit trail and a metric, and the post-hoc seat
keeps the whole-run view.

Decisions: D45 (SPEC.md).
"""

import json
import re
from typing import Any

from resgraph.sentinel.rules import INJECTION_SIGNATURES

_COMPILED = tuple(re.compile(p) for p in INJECTION_SIGNATURES)


def screen(messages: list[dict[str, Any]], system: Any = None) -> list[str]:
    """The signature patterns matched by the request's own text."""
    text = json.dumps(messages)
    if system is not None:
        text += json.dumps(system)
    return [p.pattern for p in _COMPILED if p.search(text)]
