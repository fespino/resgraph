"""The analyst loop — visible control flow, budgets in code.

A run is: system blocks (static prefix, cache breakpoint at its end) +
one user message carrying the alert; assistant turns request tools
until they conclude with a JSON report. The transcript only ever
grows — tool results, budget refusals, and validation feedback all
append as new messages, and assistant content (thinking blocks
included) is replayed verbatim. Nothing edits an earlier message: an
edited byte anywhere before the cache breakpoint would bust the cache
on exactly the runs that need retries most.

Exhausting a budget is not an error: tool requests past the ceiling
are refused with an instruction to conclude, and the run is marked
degraded.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from resgraph.schema import ResourceType

from .models import TriageReport
from .tools import ToolOutcome

MAX_TOOL_CALLS = 15
MAX_RUN_TOKENS = 150_000
MAX_VALIDATION_RETRIES = 2

_ID_RE = re.compile(rf"\b(?:{'|'.join(sorted(t.value for t in ResourceType))})-\d{{6}}\b")

_EXHAUSTED = (
    "Tool budget exhausted. Conclude now from the evidence already "
    "gathered and set degraded=true in the report."
)


class Toolset(Protocol):
    def blocks(self) -> list[dict[str, Any]]:
        """Anthropic tool blocks for the API request."""
        raise NotImplementedError

    def execute(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool call in-process and report its outcome."""
        raise NotImplementedError


@dataclass(frozen=True)
class Prompt:
    """Content is supplied by the prompt module, never authored here:
    the harness owns message order, the prompt owns words."""

    system: list[dict[str, Any]]
    user: str


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, u: Any) -> None:
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_creation_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def spent(self) -> int:
        return self.total_input + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_read_tokens / self.total_input if self.total_input else 0.0


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    ok: bool
    payload: str


@dataclass
class RunResult:
    report: TriageReport | None
    degraded: bool
    tool_calls: int
    turns: int
    usage: Usage
    trace: list[ToolCall] = field(default_factory=list)
    validation_failures: list[str] = field(default_factory=list)


def _ids(text: str) -> set[str]:
    return set(_ID_RE.findall(text))


def _extract_json(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def parse_and_validate(text: str, seen: set[str]) -> tuple[TriageReport | None, list[str]]:
    """Schema first, then referential honesty: a report citing a resource
    the run never saw fails here, before any grader."""
    raw = _extract_json(text)
    if raw is None:
        return None, ["no JSON object found in the reply"]
    try:
        report = TriageReport.model_validate_json(raw)
    except ValidationError as e:
        return None, [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
    errors = [
        f"suspect {i} cites {rid}, which appeared nowhere in this run's context or tool results"
        for i, s in enumerate(report.suspects)
        for rid in sorted({s.resource_id, *s.mechanism_path})
        if rid not in seen
    ]
    return (None, errors) if errors else (report, [])


def _feedback(errors: list[str]) -> str:
    return (
        "The report failed validation:\n- "
        + "\n- ".join(errors)
        + "\nReturn the corrected JSON report and nothing else."
    )


def run_triage(
    prompt: Prompt,
    toolset: Toolset,
    client: Any,
    *,
    model: str,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_run_tokens: int = MAX_RUN_TOKENS,
    max_tokens: int = 8_000,
    thinking: dict[str, Any] | None = None,
) -> RunResult:
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt.user}]
    seen = _ids(prompt.user)
    for block in prompt.system:
        seen |= _ids(str(block.get("text", "")))

    usage = Usage()
    trace: list[ToolCall] = []
    failures: list[str] = []
    report: TriageReport | None = None
    calls_used = 0
    retries = 0
    degraded = False
    turns = 0
    # Terminator for a model that neither concludes nor stops calling
    # tools; ordinary runs end well before this.
    turn_cap = max_tool_calls + MAX_VALIDATION_RETRIES + 5

    while turns < turn_cap:
        turns += 1
        kwargs: dict[str, Any] = {"thinking": thinking} if thinking is not None else {}
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=prompt.system,
            tools=toolset.blocks(),
            messages=messages,
            **kwargs,
        )
        usage.add(resp.usage)
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]

        if tool_uses:
            messages.append({"role": "assistant", "content": resp.content})
            results: list[dict[str, Any]] = []
            for tu in tool_uses:
                if calls_used >= max_tool_calls or usage.spent >= max_run_tokens:
                    degraded = True
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": _EXHAUSTED,
                            "is_error": True,
                        }
                    )
                    continue
                calls_used += 1
                args = dict(tu.input)
                outcome = toolset.execute(tu.name, args)
                trace.append(ToolCall(tu.name, args, outcome.ok, outcome.payload))
                if outcome.ok:
                    seen |= _ids(outcome.payload)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": outcome.payload,
                        "is_error": not outcome.ok,
                    }
                )
            messages.append({"role": "user", "content": results})
            continue

        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        report, errors = parse_and_validate(text, seen)
        if report is not None:
            break
        failures.extend(errors)
        if retries >= MAX_VALIDATION_RETRIES:
            break
        retries += 1
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": _feedback(errors)})

    return RunResult(
        report=report,
        degraded=degraded or bool(report and report.degraded),
        tool_calls=calls_used,
        turns=turns,
        usage=usage,
        trace=trace,
        validation_failures=failures,
    )
