"""Tool-surface adapter: present an Anthropic ``messages.create`` face over the
chat-completions wire format — the API that vLLM, Ollama, and llama.cpp expose
(commonly called OpenAI-compatible) — so the analyst harness can drive any such
model without knowing it is not Anthropic.

The harness speaks one vocabulary — ``resp.content`` blocks (``text`` /
``thinking`` / ``tool_use``) and ``tool_result`` message parts. Everything here
translates that vocabulary to and from the chat-completions shape
(``tool_calls`` on the assistant message, ``role:tool`` messages carrying
results). The transport is the only I/O; the translators are pure so the whole
adapter is exercised offline against fixtures.
"""

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
LineTransport = Callable[[str, dict[str, Any], dict[str, str]], Iterator[str]]


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class ThinkingBlock:
    thinking: str
    type: str = "thinking"


@dataclass
class Response:
    content: list[Any]
    usage: Usage
    stop_reason: str = "end_turn"
    source: str | None = None
    backend: str | None = None
    cached: bool = False


_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def _field(block: Any, name: str) -> Any:
    """Read a field whether the block is a dataclass, an Anthropic SDK object,
    or a plain dict — the harness mixes all three into ``messages``."""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def to_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic ``{name, description, input_schema}`` → a chat-completions function tool."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _assistant_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        kind = _field(block, "type")
        if kind == "text":
            texts.append(_field(block, "text") or "")
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": _field(block, "id"),
                    "type": "function",
                    "function": {
                        "name": _field(block, "name"),
                        "arguments": json.dumps(_field(block, "input") or {}),
                    },
                }
            )
        # thinking blocks are dropped: the chat-completions shape has no equivalent and a
        # local model does not replay them.
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _user_messages(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    tool_msgs: list[dict[str, Any]] = []
    texts: list[str] = []
    for part in content:
        kind = _field(part, "type")
        if kind == "tool_result":
            payload = _field(part, "content")
            if _field(part, "is_error"):
                payload = f"[tool error] {payload}"
            tool_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": _field(part, "tool_use_id"),
                    "content": payload,
                }
            )
        elif kind == "text":
            texts.append(_field(part, "text") or "")
    out: list[dict[str, Any]] = []
    if texts:
        out.append({"role": "user", "content": "".join(texts)})
    out.extend(tool_msgs)
    return out


def to_chat_messages(
    system: str | list[Any] | None, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        if isinstance(system, list):
            # Anthropic block-list system (cache_control riding the blocks)
            # flattens to text: the chat shape has no block system and no
            # provider-side prefix cache for the marks to address.
            system = "".join(_field(b, "text") or "" for b in system)
        out.append({"role": "system", "content": system})
    for m in messages:
        if m["role"] == "assistant":
            out.append(_assistant_message(m["content"]))
        else:
            out.extend(_user_messages(m["content"]))
    return out


def from_chat_response(data: dict[str, Any]) -> Response:
    msg = data["choices"][0]["message"]
    blocks: list[Any] = []
    if msg.get("content"):
        blocks.append(TextBlock(text=msg["content"]))
    for tc in msg.get("tool_calls") or []:
        fn = tc["function"]
        blocks.append(
            ToolUseBlock(
                id=tc["id"],
                name=fn["name"],
                input=json.loads(fn.get("arguments") or "{}"),
            )
        )
    u = data.get("usage") or {}
    usage = Usage(
        input_tokens=u.get("prompt_tokens", 0) or 0,
        output_tokens=u.get("completion_tokens", 0) or 0,
    )
    finish = (data["choices"][0].get("finish_reason") or "stop") if data.get("choices") else "stop"
    return Response(content=blocks, usage=usage, stop_reason=_STOP.get(finish, finish))


def _httpx_transport(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    resp = httpx.post(url, json=payload, headers=headers, timeout=600.0)
    resp.raise_for_status()
    return resp.json()


def _httpx_line_transport(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> Iterator[str]:
    with httpx.stream("POST", url, json=payload, headers=headers, timeout=600.0) as resp:
        resp.raise_for_status()
        yield from resp.iter_lines()


class _Messages:
    """Exposes ``.create`` so the client mimics ``anthropic.Anthropic().messages``.

    Determinism is pinned here (``temperature``, ``seed``) because a measured
    eval run must not drift between calls; the harness never overrides it.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        temperature: float,
        seed: int | None,
        tool_choice: str,
        extra_args: dict[str, Any],
        transport: Transport,
        line_transport: LineTransport,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._temperature = temperature
        self._seed = seed
        self._tool_choice = tool_choice
        self._extra_args = extra_args
        self._transport = transport
        self._line_transport = line_transport

    def _payload(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system: str | None,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        # Provider-specific FORWARD args (e.g. constrained/grammar decoding for
        # tool calls) travel through extra_args, merged last so they can set or
        # override request fields.
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": self._temperature,
            "messages": to_chat_messages(system, messages),
        }
        if self._seed is not None:
            payload["seed"] = self._seed
        if tools:
            payload["tools"] = to_chat_tools(tools)
            payload["tool_choice"] = self._tool_choice
        payload.update(self._extra_args)
        return payload

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **_ignored: Any,
    ) -> Response:
        # _ignored drops Anthropic-only kwargs (e.g. thinking) the shared
        # harness may still pass, which this backend has no equivalent for.
        payload = self._payload(
            model=model, max_tokens=max_tokens, messages=messages, system=system, tools=tools
        )
        return from_chat_response(self._transport(self._url, payload, self._headers()))

    def stream_lines(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **_ignored: Any,
    ) -> Iterator[str]:
        """The same request as ``create``, streamed: raw SSE lines out. One
        payload builder serves both, so streamed calls keep every setup knob
        (extra_args above all — the constrained-decoding channel)."""
        payload = self._payload(
            model=model, max_tokens=max_tokens, messages=messages, system=system, tools=tools
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        return self._line_transport(self._url, payload, self._headers())


class ChatCompletionsClient:
    """An Anthropic-shaped client backed by a chat-completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        tool_choice: str = "auto",
        extra_args: dict[str, Any] | None = None,
        transport: Transport | None = None,
        line_transport: LineTransport | None = None,
    ) -> None:
        self.messages = _Messages(
            url=base_url.rstrip("/") + "/chat/completions",
            api_key=api_key or os.environ.get("RESGRAPH_LOCAL_API_KEY", ""),
            temperature=temperature,
            seed=seed,
            tool_choice=tool_choice,
            extra_args=extra_args or {},
            transport=transport or _httpx_transport,
            line_transport=line_transport or _httpx_line_transport,
        )


class GatewayClient:
    """An Anthropic-shaped client that serves through the gateway.

    Routing belongs to the SETUP (``pin`` / ``task_class`` / ``alias``),
    resolved by the gateway's registry — the harness's ``model`` kwarg is a
    raw provider id the gateway does not accept, so it is ignored here and
    the setup's own routing decides. A measured run's setup pins and sets
    ``cache_responses: false`` (the instrument bypass). The winning source,
    serving backend, and cache state ride back on the Response."""

    def __init__(
        self,
        *,
        base_url: str,
        pin: str | None = None,
        task_class: str | None = None,
        alias: str | None = None,
        cache_responses: bool = True,
        transport: Transport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/v1/generate"
        self._pin = pin
        self._task_class = task_class
        self._alias = alias
        self._cache_responses = cache_responses
        self._transport = transport or _httpx_transport
        self.messages = self

    def create(
        self,
        *,
        max_tokens: int,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        **_ignored: Any,
    ) -> Response:
        body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
        if system is not None:
            body["system"] = system
        if tools is not None:
            body["tools"] = tools
        if self._pin is not None:
            body["pin"] = self._pin
        if self._task_class is not None:
            body["task_class"] = self._task_class
        if self._alias is not None:
            body["model"] = self._alias
        if not self._cache_responses:
            body["cache_responses"] = False
        data = self._transport(self._url, body, {})
        blocks: list[Any] = []
        for block in data["content"]:
            kind = block.get("type")
            if kind == "tool_use":
                blocks.append(
                    ToolUseBlock(id=block["id"], name=block["name"], input=block["input"])
                )
            elif kind == "thinking":
                blocks.append(ThinkingBlock(thinking=block.get("thinking", "")))
            else:
                blocks.append(TextBlock(text=block.get("text", "")))
        u = data["usage"]
        return Response(
            content=blocks,
            usage=Usage(
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
                cache_read_input_tokens=u.get("cache_read_tokens", 0),
                cache_creation_input_tokens=u.get("cache_creation_tokens", 0),
            ),
            source=data.get("source"),
            backend=data.get("backend"),
            cached=data.get("cached", False),
        )


def _build_gateway(setup: dict[str, Any]) -> Any:
    base_url = setup.get("base_url")
    if not base_url:
        raise SystemExit(f"setup {setup.get('name')!r} (provider gateway) needs a base_url")
    return GatewayClient(
        base_url=base_url,
        pin=setup.get("pin"),
        task_class=setup.get("task_class"),
        alias=setup.get("alias"),
        cache_responses=setup.get("cache_responses", True),
    )


def load_setup(name: str, path: Path) -> dict[str, Any]:
    """Read a named client setup from the YAML config. Secrets are never here —
    keys come from the environment (``api_key_env`` names the variable); the file
    holds only provider, model, endpoint, and determinism knobs."""
    setups = yaml.safe_load(path.read_text()) or {}
    if name not in setups:
        raise SystemExit(f"no setup {name!r} in {path}; have: {', '.join(sorted(setups))}")
    return {"name": name, **setups[name]}


def _build_anthropic(setup: dict[str, Any]) -> Any:
    from anthropic import Anthropic

    return Anthropic()


def _build_chat_completions(setup: dict[str, Any]) -> Any:
    base_url = setup.get("base_url")
    if not base_url:
        raise SystemExit(
            f"setup {setup.get('name')!r} (provider {setup.get('provider')!r}) needs a base_url"
        )
    key_env = setup.get("api_key_env")
    return ChatCompletionsClient(
        base_url=base_url,
        api_key=os.environ.get(key_env) if key_env else None,
        temperature=setup.get("temperature", 0.0),
        seed=setup.get("seed"),
        tool_choice=setup.get("tool_choice", "auto"),
        extra_args=setup.get("extra_args"),
    )


# Only Anthropic needs its own entry — its messages API carries caching and
# thinking the harness uses; every other provider falls through to chat-completions.
CLIENTS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "anthropic": _build_anthropic,
    "gateway": _build_gateway,
}


def build_client(setup: dict[str, Any]) -> Any:
    """Resolve a client from a setup, picking by ``provider`` and falling back to
    chat-completions for anything without its own builder. Role-neutral — worker
    or judge resolve the same way, so the judge is not tied to a provider. The
    setup itself is the provenance the caller records; a secret is never inline
    (``api_key_env`` names the environment variable)."""
    if "api_key" in setup:
        raise SystemExit(
            "setup carries an inline api_key; keep the secret in the environment "
            "and name it with api_key_env instead"
        )
    provider = setup.get("provider", "default")
    return CLIENTS.get(provider, _build_chat_completions)(setup)
