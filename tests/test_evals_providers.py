"""The tool-surface adapter: Anthropic message vocabulary <-> chat-completions shape."""

import json

import pytest

from resgraph.analyst.harness import Usage as HarnessUsage
from resgraph.evals.providers import (
    ChatCompletionsClient,
    TextBlock,
    ToolUseBlock,
    build_client,
    from_chat_response,
    load_setup,
    to_chat_messages,
    to_chat_tools,
)


def test_tools_map_to_function_tool_shape():
    anthropic = [
        {"name": "blast_radius", "description": "downstream", "input_schema": {"type": "object"}}
    ]
    assert to_chat_tools(anthropic) == [
        {
            "type": "function",
            "function": {
                "name": "blast_radius",
                "description": "downstream",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_system_is_prepended_as_a_message():
    out = to_chat_messages("be terse", [{"role": "user", "content": "hi"}])
    assert out[0] == {"role": "system", "content": "be terse"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_no_system_means_no_system_message():
    out = to_chat_messages(None, [{"role": "user", "content": "hi"}])
    assert out == [{"role": "user", "content": "hi"}]


def test_assistant_tool_use_becomes_tool_calls():
    content = [
        TextBlock(text="calling now"),
        ToolUseBlock(id="tu_1", name="blast_radius", input={"resource": "db-07"}),
    ]
    out = to_chat_messages(None, [{"role": "assistant", "content": content}])
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "calling now"
    (call,) = msg["tool_calls"]
    assert call["id"] == "tu_1"
    assert call["function"]["name"] == "blast_radius"
    assert json.loads(call["function"]["arguments"]) == {"resource": "db-07"}


def test_assistant_tool_use_only_has_null_content():
    content = [ToolUseBlock(id="tu_1", name="t", input={})]
    (msg,) = to_chat_messages(None, [{"role": "assistant", "content": content}])
    assert msg["content"] is None
    assert len(msg["tool_calls"]) == 1


def test_assistant_blocks_may_be_plain_dicts():
    # The harness sometimes appends dict-shaped blocks, not dataclasses.
    content = [{"type": "tool_use", "id": "tu_9", "name": "t", "input": {"a": 1}}]
    (msg,) = to_chat_messages(None, [{"role": "assistant", "content": content}])
    assert msg["tool_calls"][0]["id"] == "tu_9"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


def test_tool_result_becomes_role_tool_message():
    content = [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "3 nodes", "is_error": False}
    ]
    (msg,) = to_chat_messages(None, [{"role": "user", "content": content}])
    assert msg == {"role": "tool", "tool_call_id": "tu_1", "content": "3 nodes"}


def test_tool_result_error_is_marked_in_content():
    content = [{"type": "tool_result", "tool_use_id": "tu_1", "content": "boom", "is_error": True}]
    (msg,) = to_chat_messages(None, [{"role": "user", "content": content}])
    assert msg["content"] == "[tool error] boom"


def test_multiple_tool_results_keep_order():
    content = [
        {"type": "tool_result", "tool_use_id": "a", "content": "first", "is_error": False},
        {"type": "tool_result", "tool_use_id": "b", "content": "second", "is_error": False},
    ]
    out = to_chat_messages(None, [{"role": "user", "content": content}])
    assert [m["tool_call_id"] for m in out] == ["a", "b"]


def test_user_text_block_becomes_user_message():
    content = [{"type": "text", "text": "conclude now"}]
    (msg,) = to_chat_messages(None, [{"role": "user", "content": content}])
    assert msg == {"role": "user", "content": "conclude now"}


def test_from_chat_response_text_only():
    data = {
        "choices": [{"message": {"content": "the answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    resp = from_chat_response(data)
    assert [b.type for b in resp.content] == ["text"]
    assert resp.content[0].text == "the answer"
    assert resp.stop_reason == "end_turn"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (100, 20)


def test_from_chat_response_tool_calls_parse_arguments():
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "blast_radius",
                                "arguments": '{"resource": "db-07"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    resp = from_chat_response(data)
    (block,) = resp.content
    assert isinstance(block, ToolUseBlock)
    assert block.name == "blast_radius"
    assert block.input == {"resource": "db-07"}
    assert resp.stop_reason == "tool_use"


def test_response_usage_plugs_into_harness_accounting():
    data = {
        "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 8},
    }
    resp = from_chat_response(data)
    acc = HarnessUsage()
    acc.add(resp.usage)  # cache_* fields absent -> counted as zero, no crash
    assert acc.input_tokens == 40
    assert acc.output_tokens == 8
    assert acc.cache_read_tokens == 0


def test_client_full_roundtrip_through_a_fake_transport():
    captured: dict[str, object] = {}

    def fake_transport(url, payload, headers):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "blast_radius", "arguments": '{"r": 1}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        }

    client = ChatCompletionsClient(
        base_url="http://localhost:11434/v1",
        api_key="k",
        seed=42,
        transport=fake_transport,
    )
    resp = client.messages.create(
        model="qwen2.5",
        max_tokens=256,
        system="sys",
        tools=[{"name": "blast_radius", "description": "d", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "investigate"}],
        thinking={"type": "adaptive"},  # Anthropic-only kwarg: must be ignored
    )
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    payload = captured["payload"]
    assert payload["model"] == "qwen2.5"
    assert payload["temperature"] == 0.0
    assert payload["seed"] == 42
    assert payload["tool_choice"] == "auto"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert captured["headers"]["Authorization"] == "Bearer k"
    (block,) = resp.content
    assert isinstance(block, ToolUseBlock)
    assert block.name == "blast_radius"


def test_client_omits_tools_and_seed_when_unset():
    captured: dict[str, object] = {}

    def fake_transport(url, payload, headers):
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    client = ChatCompletionsClient(base_url="http://x/v1", transport=fake_transport)
    client.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "hi"}])
    payload = captured["payload"]
    assert "tools" not in payload
    assert "seed" not in payload
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_extra_args_merge_into_the_payload():
    captured: dict[str, object] = {}

    def fake_transport(url, payload, headers):
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    client = ChatCompletionsClient(
        base_url="http://x/v1",
        extra_args={"guided_json": {"type": "object"}, "tool_choice": "required"},
        transport=fake_transport,
    )
    client.messages.create(
        model="m",
        max_tokens=8,
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "t", "description": "d", "input_schema": {}}],
    )
    payload = captured["payload"]
    assert payload["guided_json"] == {"type": "object"}
    # extra_args is merged last, so a provider-specific value wins over the default.
    assert payload["tool_choice"] == "required"


def test_build_client_endpoint_uses_the_chat_completions_client():
    setup = {"provider": "ollama", "model": "qwen2.5:7b", "base_url": "http://a-remote-gpu:8000/v1"}
    assert isinstance(build_client(setup), ChatCompletionsClient)


def test_build_client_anthropic_provider_uses_the_sdk(monkeypatch):
    import anthropic

    sentinel = object()
    monkeypatch.setattr(anthropic, "Anthropic", lambda: sentinel)
    assert build_client({"provider": "anthropic", "model": "claude-opus-4-8"}) is sentinel


def test_unknown_provider_falls_back_to_chat_completions():
    client = build_client({"provider": "together", "model": "x", "base_url": "http://x/v1"})
    assert isinstance(client, ChatCompletionsClient)


def test_chat_provider_without_a_base_url_is_rejected():
    with pytest.raises(SystemExit, match="needs a base_url"):
        build_client({"name": "oops", "provider": "openai", "model": "gpt-4o"})


def test_load_setup_reads_a_named_setup(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("qwen:\n  provider: ollama\n  model: qwen2.5:7b\n  base_url: http://x/v1\n")
    assert load_setup("qwen", cfg) == {
        "name": "qwen",
        "provider": "ollama",
        "model": "qwen2.5:7b",
        "base_url": "http://x/v1",
    }


def test_load_setup_rejects_an_unknown_name(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("opus:\n  provider: anthropic\n  model: claude-opus-4-8\n")
    with pytest.raises(SystemExit, match="no setup 'nope'"):
        load_setup("nope", cfg)


def test_build_client_reads_the_key_from_the_named_env_var(monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_KEY", "sk-from-env")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "resgraph.evals.providers.ChatCompletionsClient",
        lambda **kw: captured.update(kw) or object(),
    )
    build_client(
        {
            "name": "hosted",
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": "https://api.example.com/v1",
            "api_key_env": "SOME_PROVIDER_KEY",
        }
    )
    assert captured["api_key"] == "sk-from-env"


def test_build_client_refuses_an_inline_secret():
    with pytest.raises(SystemExit, match="api_key_env"):
        build_client(
            {
                "provider": "openai",
                "model": "gpt-4o",
                "base_url": "http://x/v1",
                "api_key": "sk-leak",
            }
        )


def test_stream_lines_shares_the_payload_builder_with_create():
    seen: dict = {}

    def lines(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        yield "data: [DONE]"

    client = ChatCompletionsClient(
        base_url="http://localhost:1/v1",
        seed=7,
        extra_args={"guided_json": {"type": "object"}},
        line_transport=lines,
    )
    out = list(
        client.messages.stream_lines(
            model="qwen2.5:1.5b",
            max_tokens=64,
            messages=[{"role": "user", "content": "q"}],
            tools=[{"name": "fetch_resource", "input_schema": {}}],
            thinking={"type": "adaptive"},
        )
    )
    assert out == ["data: [DONE]"]
    payload = seen["payload"]
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["seed"] == 7
    assert payload["guided_json"] == {"type": "object"}
    assert payload["tools"][0]["function"]["name"] == "fetch_resource"
    assert "thinking" not in payload


def test_a_block_list_system_flattens_to_text_for_the_chat_shape():
    system = [
        {"type": "text", "text": "prefix ", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "and rules"},
    ]
    out = to_chat_messages(system, [{"role": "user", "content": "q"}])
    assert out[0] == {"role": "system", "content": "prefix and rules"}
