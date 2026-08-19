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
degraded. Cost and wall-clock ceilings (D29) follow the same
philosophy at the turn boundary — never mid-call, the same reason
D28's cancel never lands mid-step: a breach injects one final
"conclude now" turn whose answer is the report. An exception is not
a conclusion.

Decisions: D22, D23 (SPEC.md).
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
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
_SEQ_RE = re.compile(r'"sequence":\s*(\d+)')

_EXHAUSTED = (
    "Tool budget exhausted. Conclude now from the evidence already "
    "gathered and set degraded=true in the report."
)
_CONCLUDE_NOW = (
    "Budget exhausted — stop investigating. Conclude now from the "
    "evidence you already hold, mark confidence at what that evidence "
    "actually earns, and set degraded=true in the report."
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

    @property
    def uncached_fraction(self) -> float:
        """Input billed at full price — neither read from nor written
        to cache."""
        return self.input_tokens / self.total_input if self.total_input else 0.0


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
    cutoff_reason: str | None = None  # tool_calls | tokens | cost | wall_clock


def cache_fingerprint(prompt: Prompt, toolset: Toolset) -> str:
    """SHA-256 over everything that serializes before the cache
    breakpoint: tool blocks plus the prefix system blocks. Recorded
    next to the cache-hit metric so a cache regression is
    self-diagnosing — an unchanged fingerprint means runtime behavior,
    a changed one means a registry or prompt edit busted the cache."""
    prefix: list[dict[str, Any]] = []
    for block in prompt.system:
        prefix.append(block)
        if "cache_control" in block:
            break
    payload = json.dumps(
        {"tools": toolset.blocks(), "system_prefix": prefix}, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _ids(text: str) -> set[str]:
    return set(_ID_RE.findall(text))


def _extract_json(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


def parse_and_validate(
    text: str, seen: set[str], seen_sequences: frozenset[int] | set[int] = frozenset()
) -> tuple[TriageReport | None, list[str]]:
    """Schema first, then referential honesty, then verdict arithmetic:
    a report citing resources or events the run never saw fails here,
    and the abstention flag must equal what the verdicts imply — the
    flag is derived, not chosen."""
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
    for i, s in enumerate(report.suspects):
        if s.verdict.event_found and s.sequence == 0:
            errors.append(
                f"suspect {i}: event_found=true but sequence 0 is the initial "
                "snapshot, not a change event"
            )
        elif s.verdict.event_found and s.sequence not in seen_sequences:
            errors.append(
                f"suspect {i}: event_found=true but sequence {s.sequence} never "
                "appeared in this run's tool results"
            )
    confident = any(s.verdict.confident for s in report.suspects)
    if report.no_confident_candidate == confident:
        errors.append(
            "no_confident_candidate must be exactly (no suspect has all three "
            f"verdicts true); emitted {report.no_confident_candidate} while a "
            f"fully-confident suspect present={confident}"
        )
    return (None, errors) if errors else (report, [])


def _feedback(errors: list[str]) -> str:
    return (
        "The report failed validation:\n- "
        + "\n- ".join(errors)
        + "\nReturn the corrected JSON report and nothing else."
    )


def _mark_transcript_breakpoint(messages: list[dict[str, Any]]) -> None:
    """One moving breakpoint on the last block of the last user message,
    so the growing transcript caches incrementally. Marks are metadata:
    moving one changes no content bytes, and message dicts are mutated
    in place so replayed objects keep their identity."""
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]["content"]
    if isinstance(last, list) and last and isinstance(last[-1], dict):
        last[-1]["cache_control"] = {"type": "ephemeral"}


def run_triage(
    prompt: Prompt,
    toolset: Toolset,
    client: Any,
    *,
    model: str,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_run_tokens: int = MAX_RUN_TOKENS,
    max_tokens: int = 8_000,
    extra_args: dict[str, Any] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    max_cost_usd: float | None = None,
    cost_fn: Callable[[Usage], float] | None = None,
    max_wall_s: float | None = None,
) -> RunResult:
    if (max_cost_usd is None) != (cost_fn is None):
        raise ValueError("max_cost_usd and cost_fn come together: the ceiling needs its meter")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": prompt.user}]}
    ]
    # Citable ids come from run data only: the user message and the
    # suffix blocks after the cache breakpoint. Prefix text (skill
    # bodies, worked examples) carries illustrative ids that must not
    # pass referential validation.
    seen = _ids(prompt.user)
    past_breakpoint = False
    for block in prompt.system:
        if past_breakpoint:
            seen |= _ids(str(block.get("text", "")))
        if "cache_control" in block:
            past_breakpoint = True

    usage = Usage()
    trace: list[ToolCall] = []
    failures: list[str] = []
    seen_sequences: set[int] = set()
    report: TriageReport | None = None
    calls_used = 0
    retries = 0
    degraded = False
    turns = 0
    cutoff_reason: str | None = None
    concluding = False
    run_started = time.monotonic()
    # Terminator for a model that neither concludes nor stops calling
    # tools; ordinary runs end well before this.
    turn_cap = max_tool_calls + MAX_VALIDATION_RETRIES + 5

    while turns < turn_cap:
        turns += 1
        if not concluding:
            breach = None
            if cost_fn is not None and max_cost_usd is not None and cost_fn(usage) >= max_cost_usd:
                breach = "cost"
            elif max_wall_s is not None and time.monotonic() - run_started >= max_wall_s:
                breach = "wall_clock"
            if breach is not None:
                concluding = True
                degraded = True
                cutoff_reason = breach
                if on_event is not None:
                    on_event(
                        "cutoff",
                        {
                            "reason": breach,
                            "calls_used": calls_used,
                            "tokens_spent": usage.spent,
                        },
                    )
                messages.append(
                    {"role": "user", "content": [{"type": "text", "text": _CONCLUDE_NOW}]}
                )
        _mark_transcript_breakpoint(messages)
        kwargs: dict[str, Any] = {**(extra_args or {})}
        llm_started = time.monotonic()
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
        if on_event is not None:
            # A monitor can only read what the trail recorded: reasoning
            # blocks land in the audit row when the API returns them.
            # The response shape cannot distinguish full CoT from a
            # summary, so the form labels what we can see: recorded
            # (text present), elided (blocks without text), absent.
            reasoning = [
                t
                for b in resp.content
                if getattr(b, "type", None) == "thinking"
                if (t := getattr(b, "thinking", "") or "")
            ]
            had_blocks = any(getattr(b, "type", None) == "thinking" for b in resp.content)
            event: dict[str, Any] = {
                "turn": turns,
                "tool_uses": len(tool_uses),
                "thinking_form": "recorded" if reasoning else "elided" if had_blocks else "absent",
                "latency_ms": int((time.monotonic() - llm_started) * 1000),
                "tokens": (getattr(resp.usage, "input_tokens", 0) or 0)
                + (getattr(resp.usage, "output_tokens", 0) or 0),
            }
            if (route_source := getattr(resp, "source", None)) is not None:
                # served through the gateway: the winning routing source,
                # backend, and cache state land in the trail per call
                event["source"] = route_source
                event["backend"] = getattr(resp, "backend", None)
                event["cached"] = getattr(resp, "cached", False)
            if reasoning:
                event["thinking"] = "\n\n".join(reasoning)
            on_event("llm_call", event)

        if tool_uses:
            messages.append({"role": "assistant", "content": resp.content})
            results: list[dict[str, Any]] = []
            for tu in tool_uses:
                if calls_used >= max_tool_calls or usage.spent >= max_run_tokens or concluding:
                    if not degraded:
                        cutoff_reason = "tool_calls" if calls_used >= max_tool_calls else "tokens"
                        if on_event is not None:
                            on_event(
                                "cutoff",
                                {
                                    "reason": cutoff_reason,
                                    "calls_used": calls_used,
                                    "tokens_spent": usage.spent,
                                },
                            )
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
                tool_started = time.monotonic()
                outcome = toolset.execute(tu.name, args)
                trace.append(ToolCall(tu.name, args, outcome.ok, outcome.payload))
                if outcome.ok:
                    seen |= _ids(outcome.payload)
                    seen_sequences |= {int(m) for m in _SEQ_RE.findall(outcome.payload)}
                if on_event is not None:
                    on_event(
                        "tool_call",
                        {
                            "tool": tu.name,
                            "args": args,
                            "ok": outcome.ok,
                            "ids": sorted(_ids(outcome.payload)) if outcome.ok else [],
                            "latency_ms": int((time.monotonic() - tool_started) * 1000),
                        },
                    )
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
        report, errors = parse_and_validate(text, seen, seen_sequences)
        if report is not None:
            break
        failures.extend(errors)
        if retries >= MAX_VALIDATION_RETRIES:
            break
        retries += 1
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": [{"type": "text", "text": _feedback(errors)}]})

    return RunResult(
        report=report,
        degraded=degraded or bool(report and report.degraded),
        tool_calls=calls_used,
        turns=turns,
        usage=usage,
        trace=trace,
        validation_failures=failures,
        cutoff_reason=cutoff_reason,
    )
