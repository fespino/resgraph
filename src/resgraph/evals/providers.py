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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


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


@dataclass
class Response:
    content: list[Any]
    usage: Usage
    stop_reason: str = "end_turn"


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


def to_chat_messages(system: str | None, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
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
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._temperature = temperature
        self._seed = seed
        self._tool_choice = tool_choice
        self._extra_args = extra_args
        self._transport = transport

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
        # Provider-specific FORWARD args (e.g. constrained/grammar decoding for
        # tool calls) travel through extra_args, merged last so they can set or
        # override request fields. _ignored drops Anthropic-only kwargs (e.g.
        # thinking) the shared harness may still pass, which this backend has no
        # equivalent for.
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
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        return from_chat_response(self._transport(self._url, payload, headers))


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
    ) -> None:
        self.messages = _Messages(
            url=base_url.rstrip("/") + "/chat/completions",
            api_key=api_key or os.environ.get("RESGRAPH_LOCAL_API_KEY", ""),
            temperature=temperature,
            seed=seed,
            tool_choice=tool_choice,
            extra_args=extra_args or {},
            transport=transport or _httpx_transport,
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
CLIENTS: dict[str, Callable[[dict[str, Any]], Any]] = {"anthropic": _build_anthropic}


def build_client(setup: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Resolve a client and its provenance from a setup, picking by ``provider``
    and falling back to chat-completions for anything without its own builder.
    Role-neutral — worker or judge resolve the same way, so the judge is not tied
    to a provider (the caller keeps it constant across arms)."""
    if "api_key" in setup:
        raise SystemExit(
            "setup carries an inline api_key; keep the secret in the environment "
            "and name it with api_key_env instead"
        )
    provider = setup.get("provider")
    client = CLIENTS.get(provider or "", _build_chat_completions)(setup)
    meta: dict[str, Any] = {"provider": provider}
    if setup.get("name"):
        meta["setup"] = setup["name"]
    if setup.get("base_url"):
        meta |= {
            "base_url": setup["base_url"],
            "temperature": setup.get("temperature", 0.0),
            "seed": setup.get("seed"),
            "quant": setup.get("quant"),
        }
    return client, meta
