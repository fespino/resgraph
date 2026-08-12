"""Mechanical sanitization validators for dataset items (D24).

The code half of evals/scenarios/SANITIZATION.md: every check with a
computable arbiter lives here, called by the mining script before an
item is written and by the CI sweep over committed datasets — one
implementation, every caller. Judgment-shaped checks (context
leakage, synthetic-only content) stay in the checklist, reviewed at
PR time.

Functional by design: a TextValidator is a named pure function over
one string, and `check_fields` runs any validator set over any
(field, text) selection. Only `scenario_fields` and
`lineage_findings` know the Scenario shape — so the scanners lift
unchanged if a second dataset family ever appears. Deliberately no
cross-dataset API until that day: a real consumer must force the
open semantics (e.g. what a missing column means) before code
answers them.

Findings are structured — validator name, field, detail — so a
failure names the check that fired and where. The secret validator
never echoes its match: quoting it would copy the leak into logs.
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from resgraph.gen.scenarios import Scenario


@dataclass(frozen=True)
class Finding:
    validator: str
    field: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.validator}] {self.field}: {self.detail}"


@dataclass(frozen=True)
class TextValidator:
    """A named pure function over one text value, reusable against any
    dataset by pairing it with that dataset's field selection."""

    name: str
    scan: Callable[[str], list[str]]


def _pattern_scanner(
    patterns: Sequence[tuple[str, re.Pattern[str]]], *, redact: bool = False
) -> Callable[[str], list[str]]:
    def scan(text: str) -> list[str]:
        return [
            f"{name} at chars {m.start()}..{m.end()}" if redact else f"{name}: {m.group(0)}"
            for name, pattern in patterns
            for m in pattern.finditer(text)
        ]

    return scan


secrets = TextValidator(
    "secret",
    _pattern_scanner(
        (
            ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9-]{8,}")),
            ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
            ("bearer-token", re.compile(r"(?i)bearer\s+[a-z0-9._=-]{16,}")),
            (
                "assignment",
                re.compile(r"(?i)\b(api_key|apikey|token|secret|password)\s*[=:]\s*\S+"),
            ),
        ),
        redact=True,
    ),
)

local_env = TextValidator(
    "local-env",
    _pattern_scanner(
        (
            ("home-path", re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+")),
            ("windows-path", re.compile(r"[A-Za-z]:\\")),
            ("mdns-host", re.compile(r"(?i)\b[a-z0-9-]+\.local\b")),
        )
    ),
)

model_names = TextValidator(
    "model-name",
    _pattern_scanner((("model", re.compile(r"(?i)\b(claude|gpt-\d|gemini|opus|sonnet|haiku)\b")),)),
)

ITEM_TEXT_VALIDATORS: tuple[TextValidator, ...] = (secrets, local_env, model_names)

_LINEAGE_KEYS = ("derived_from", "exposed_by_run", "bucket")


def check_fields(
    fields: Iterable[tuple[str, str]], validators: Sequence[TextValidator]
) -> list[Finding]:
    """Run every validator over every (field, text) pair."""
    return [
        Finding(validator.name, field, detail)
        for field, text in fields
        for validator in validators
        for detail in validator.scan(text)
    ]


def scenario_fields(spec: Scenario) -> list[tuple[str, str]]:
    fields = [("id", spec.id), ("description", spec.description)]
    fields += [(f"tags[{i}]", tag) for i, tag in enumerate(spec.tags)]
    fields += [(f"provenance.{key}", str(value)) for key, value in spec.provenance.items()]
    return fields


def lineage_findings(spec: Scenario) -> list[Finding]:
    """Record-level check (cross-field, so not a TextValidator): a
    failure-derived item must name its full lineage."""
    if spec.provenance.get("source") != "failure_derived":
        return []
    return [
        Finding("lineage", f"provenance.{key}", "missing")
        for key in _LINEAGE_KEYS
        if not spec.provenance.get(key)
    ]


def injection_findings(spec: Scenario) -> list[Finding]:
    """Injection items carry adversarial text by design (#160). The
    boundary that keeps this from becoming a smuggling channel: the
    text must equal the canonical template for its declared target, so
    nothing arbitrary can live in the field, and only injection-tagged
    items may carry it."""
    from resgraph.evals.injection import SENTINEL, injection_text

    tagged = "injection" in spec.tags
    text = str(spec.provenance.get("inject_text", ""))
    has_sentinel = SENTINEL in text or SENTINEL in spec.description
    if not tagged:
        return (
            [Finding("injection", "description", "sentinel outside an injection item")]
            if has_sentinel
            else []
        )
    target = str(spec.provenance.get("inject_target", ""))
    if not target:
        return [Finding("injection", "provenance.inject_target", "missing")]
    if text != injection_text(target):
        return [Finding("injection", "provenance.inject_text", "not the canonical template")]
    return []


def sanitize_findings(spec: Scenario) -> list[Finding]:
    return (
        check_fields(scenario_fields(spec), ITEM_TEXT_VALIDATORS)
        + lineage_findings(spec)
        + injection_findings(spec)
    )
