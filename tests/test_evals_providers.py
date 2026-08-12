"""The tool-surface adapter: Anthropic message vocabulary <-> chat-completions shape."""

import json

import pytest

from resgraph.analyst.harness import Usage as HarnessUsage
from resgraph.evals.providers import (
    ChatCompletionsClient,
    TextBlock,
    ToolUseBlock,
    build_worker,
    from_chat_response,
    load_worker,
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


def test_build_worker_endpoint_pins_full_identity():
    # A chat-completions endpoint, whether on localhost or a remote GPU.
    setup = {
        "name": "qwen-remote",
        "model": "qwen2.5:7b",
        "base_url": "http://a-remote-gpu:8000/v1",
        "seed": 7,
        "quant": "Q4_K_M",
        "temperature": 0,
    }
    client, prov = build_worker(setup)
    assert isinstance(client, ChatCompletionsClient)
    assert prov == {
        "worker_setup": "qwen-remote",
        "worker_base_url": "http://a-remote-gpu:8000/v1",
        "worker_temperature": 0,
        "worker_seed": 7,
        "worker_quant": "Q4_K_M",
    }


def test_build_worker_anthropic_records_only_the_setup_name(monkeypatch):
    import anthropic

    sentinel = object()
    monkeypatch.setattr(anthropic, "Anthropic", lambda: sentinel)
    client, prov = build_worker({"name": "opus", "model": "claude-opus-4-8"})
    assert client is sentinel
    assert prov == {"worker_setup": "opus"}


def test_build_worker_anonymous_default_records_nothing_extra(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda: object())
    _, prov = build_worker({"model": "claude-opus-4-8"})  # no name -> the model says it all
    assert prov == {}


def test_load_worker_reads_a_named_setup(tmp_path):
    cfg = tmp_path / "workers.yaml"
    cfg.write_text("qwen-local:\n  model: qwen2.5:7b\n  base_url: http://localhost:11434/v1\n")
    setup = load_worker("qwen-local", cfg)
    assert setup == {
        "name": "qwen-local",
        "model": "qwen2.5:7b",
        "base_url": "http://localhost:11434/v1",
    }


def test_load_worker_rejects_an_unknown_setup(tmp_path):
    cfg = tmp_path / "workers.yaml"
    cfg.write_text("opus:\n  model: claude-opus-4-8\n")
    with pytest.raises(SystemExit, match="no worker setup 'nope'"):
        load_worker("nope", cfg)
