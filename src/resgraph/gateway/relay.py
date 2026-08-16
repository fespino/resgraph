"""The streaming relay: backend events in, SSE lines out, honesty preserved.

The relay consumes a provider-neutral event stream — ("content", text) deltas
and a ("usage", {...}) tail — and yields wire-ready SSE lines. A failure
before the first content token joins the same fallback walk as a non-streamed
request (a zero-token death is observably identical to an init failure); a
failure after any token emitted becomes a structured ``stream_error`` event
and the stream ends — tokens already reached the client, and a replay could
splice two answers, so no resume path exists.

The clock is injected and the event iterators are plain iterators, so every
rule here runs offline."""

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

from resgraph.gateway.accounting import StreamAccount
from resgraph.gateway.dispatch import Backend

StreamEvent = tuple[str, Any]
StreamFactory = Callable[[str, dict[str, Any]], Iterator[StreamEvent]]
Reopen = Callable[[list[str]], tuple[str, Backend, Iterator[StreamEvent]] | None]
Clock = Callable[[], float]


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def parse_chat_sse(lines: Iterator[str]) -> Iterator[StreamEvent]:
    """Chat-completions SSE lines → relay events. Pure: feed it any line
    iterator. Emits one ("content", text) per delta and a ("usage", {...})
    when the final chunk carries usage."""
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        chunk = json.loads(data)
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("tool_calls"):
                # Dropping a tool call silently would make the model look like
                # it chose not to act. Loud until streamed tool assembly lands.
                raise NotImplementedError("streamed tool calls are not assembled yet")
            if delta.get("content"):
                yield ("content", delta["content"])
        usage = chunk.get("usage")
        if usage:
            yield (
                "usage",
                {
                    "input_tokens": usage.get("prompt_tokens", 0) or 0,
                    "output_tokens": usage.get("completion_tokens", 0) or 0,
                },
            )


def relay(
    *,
    alias: str,
    backend: Backend,
    events: Iterator[StreamEvent],
    source: str,
    fallback_chain: list[str],
    reopen: Reopen,
    clock: Clock = time.monotonic,
    observe: Callable[[dict[str, Any]], None] = lambda payload: None,
) -> Iterator[str]:
    """Relay one opened stream to SSE. A zero-token death restarts silently
    via ``reopen`` (which walks like an init failure and returns the next
    opened stream, or None when the walk is exhausted); a death after any
    token surfaces a ``stream_error`` and ends the stream. The admitted
    backend slot is released on every exit, including a client disconnect
    closing the generator mid-stream."""
    owned = True
    completed = False
    try:
        while True:
            account = StreamAccount(backend=backend.name, started_at=clock())
            usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0}
            try:
                for kind, payload in events:
                    if kind == "content":
                        account.content(at=clock())
                        yield sse({"type": "content", "text": payload})
                    elif kind == "usage":
                        usage = payload
            except Exception as exc:
                owned = False
                backend.release()
                death = account.died(at=clock(), reason=str(exc) or type(exc).__name__)
                if death.restartable:
                    fallback_chain.append(f"{backend.name}:{alias}")
                    reopened = reopen(fallback_chain)
                    if reopened is not None:
                        alias, backend, events = reopened
                        owned = True
                        continue
                error_payload = {
                    "type": "stream_error",
                    "backend": death.backend,
                    "reason": death.reason,
                    "tokens_emitted": death.tokens_emitted,
                    "fallback_chain": fallback_chain,
                }
                completed = True
                observe(error_payload)
                yield sse(error_payload)
                return
            reconciliation = account.finish(
                at=clock(), reported_output_tokens=usage["output_tokens"]
            )
            if account.ttft is not None:
                backend.ttft_ewma.update(account.ttft)
            owned = False
            backend.release()
            end_payload = {
                "type": "end",
                "model": alias,
                "source": source,
                "backend": backend.name,
                "fallback_chain": fallback_chain,
                "usage": usage,
                "ttft_s": account.ttft,
                "tokens_per_s": account.tokens_per_second,
                "reconciliation_ok": reconciliation.within_tolerance,
            }
            completed = True
            observe(end_payload)
            yield sse(end_payload)
            return
    finally:
        if owned:
            backend.release()
        if not completed:
            # A hung-up client is an outcome, not a silence: without this
            # the request class vanishes from the request counter entirely.
            observe({"type": "disconnect", "backend": backend.name})
