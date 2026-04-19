import json

from harness_to_mcp.adapters import (
    MAX_TEXT_LENGTH,
    AnthropicMessagesAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    ToolCallSpec,
    TurnPayload,
    tool_result_to_mcp_content,
    truncate_long_text,
)
from harness_to_mcp.server import create_app
from starlette.testclient import TestClient


def test_responses_adapter_extracts_tools_and_results() -> None:
    adapter = OpenAIResponsesAdapter()
    request = adapter.parse_request(
        {
            "model": "demo-model",
            "stream": True,
            "tools": [{"type": "function", "name": "exec_command", "description": "Run shell", "parameters": {"type": "object"}}],
            "input": [{"type": "function_call_output", "call_id": "call_1", "output": "ok"}],
        }
    )
    assert request.model == "demo-model"
    assert request.stream is True
    assert [tool.name for tool in request.tools] == ["exec_command"]
    assert request.tool_results[0].tool_call_id == "call_1"
    assert request.tool_results[0].content == "ok"


def test_responses_adapter_accepts_codex_session_id_header() -> None:
    adapter = OpenAIResponsesAdapter()
    assert adapter.session_token_from_headers({"session_id": "token-1"}) == "token-1"


def test_openai_chat_adapter_extracts_initial_prompts() -> None:
    adapter = OpenAIChatAdapter()
    request = adapter.parse_request(
        {
            "model": "demo-model",
            "stream": True,
            "tools": [{"type": "function", "name": "exec_command", "description": "Run shell", "parameters": {"type": "object"}}],
            "messages": [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "\"Hello from opencode probe\"\n"},
            ],
        }
    )
    assert request.initial_prompts is not None
    assert request.initial_prompts.instructions == "System prompt"
    assert request.initial_prompts.user_prompt == "Hello from opencode probe"
    assert request.initial_prompts.harness_context is None


def test_responses_adapter_extracts_initial_prompts() -> None:
    adapter = OpenAIResponsesAdapter()
    request = adapter.parse_request(
        {
            "model": "demo-model",
            "stream": True,
            "instructions": "Base instructions",
            "tools": [{"type": "function", "name": "exec_command", "description": "Run shell", "parameters": {"type": "object"}}],
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Developer context"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<environment_context>demo</environment_context>"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello from codex probe"}],
                },
            ],
        }
    )
    assert request.initial_prompts is not None
    assert request.initial_prompts.instructions == "Base instructions"
    assert request.initial_prompts.harness_context == "Developer context\n\n<environment_context>demo</environment_context>"
    assert request.initial_prompts.user_prompt == "Hello from codex probe"


def test_responses_adapter_builds_function_call_stream() -> None:
    adapter = OpenAIResponsesAdapter()
    payload = TurnPayload(
        model="demo-model",
        tool_calls=[ToolCallSpec(call_id="call_1", name="exec_command", arguments={"cmd": "printf hi"})],
    )
    events = adapter.build_stream_events(payload)
    assert [event["sequence_number"] for event in events] == list(range(1, len(events) + 1))
    assert next(event for event in events if event["type"] == "response.function_call_arguments.done")["name"] == "exec_command"
    chunks = adapter.build_stream_chunks(payload)
    assert chunks[-1] == b"data: [DONE]\n\n"
    combined = b"".join(chunks).decode("utf-8")
    assert "response.function_call_arguments.done" in combined
    assert '"call_id": "call_1"' in combined


def test_responses_adapter_builds_batched_function_calls() -> None:
    adapter = OpenAIResponsesAdapter()
    payload = TurnPayload(
        model="demo-model",
        tool_calls=[
            ToolCallSpec(call_id="call_1", name="exec_command", arguments={"cmd": "printf hi"}),
            ToolCallSpec(call_id="call_2", name="exec_command", arguments={"cmd": "pwd"}),
        ],
    )
    response = adapter.build_json_response(payload)
    assert response["parallel_tool_calls"] is True
    assert [item["call_id"] for item in response["output"]] == ["call_1", "call_2"]


def test_anthropic_adapter_extracts_tools_and_results() -> None:
    adapter = AnthropicMessagesAdapter()
    request = adapter.parse_request(
        {
            "model": "claude-sonnet-4-20250514",
            "stream": True,
            "tools": [{"name": "Bash", "description": "Run shell", "input_schema": {"type": "object"}}],
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "printf hi"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}]},
            ],
        }
    )
    assert [tool.name for tool in request.tools] == ["Bash"]
    assert request.tool_results[0].tool_call_id == "toolu_1"
    assert request.tool_results[0].content == "hi"


def test_anthropic_adapter_extracts_initial_prompts() -> None:
    adapter = AnthropicMessagesAdapter()
    request = adapter.parse_request(
        {
            "model": "claude-sonnet-4-20250514",
            "stream": True,
            "tools": [{"name": "Bash", "description": "Run shell", "input_schema": {"type": "object"}}],
            "system": [
                {"type": "text", "text": "System prompt"},
                {"type": "text", "text": "Additional instructions"},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<system-reminder>context</system-reminder>"},
                        {"type": "text", "text": "<|harness_to_mcp_start|> bootstrap <|harness_to_mcp_end|>"},
                    ],
                }
            ],
        }
    )
    assert request.initial_prompts is not None
    assert request.initial_prompts.instructions == "System prompt\n\nAdditional instructions"
    assert request.initial_prompts.harness_context == "<system-reminder>context</system-reminder>"
    assert request.initial_prompts.user_prompt is None


def test_anthropic_adapter_builds_tool_use_json() -> None:
    adapter = AnthropicMessagesAdapter()
    payload = TurnPayload(
        model="claude-sonnet-4-20250514",
        tool_calls=[ToolCallSpec(call_id="toolu_1", name="Bash", arguments={"cmd": "printf hi"})],
    )
    response = adapter.build_json_response(payload)
    assert response["stop_reason"] == "tool_use"
    assert response["content"][0]["type"] == "tool_use"
    assert response["content"][0]["input"]["cmd"] == "printf hi"


def test_anthropic_adapter_builds_batched_tool_use_json() -> None:
    adapter = AnthropicMessagesAdapter()
    payload = TurnPayload(
        model="claude-sonnet-4-20250514",
        tool_calls=[
            ToolCallSpec(call_id="toolu_1", name="Bash", arguments={"cmd": "printf hi"}),
            ToolCallSpec(call_id="toolu_2", name="Bash", arguments={"cmd": "pwd"}),
        ],
    )
    response = adapter.build_json_response(payload)
    assert response["stop_reason"] == "tool_use"
    assert [item["id"] for item in response["content"]] == ["toolu_1", "toolu_2"]


def test_tool_result_to_mcp_content_preserves_image_blocks() -> None:
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "data": "aGVsbG8=",
                "media_type": "image/png",
            },
        }
    ]
    result = tool_result_to_mcp_content(content)
    assert len(result) == 1
    assert result[0].type == "image"
    assert result[0].mimeType == "image/png"
    assert result[0].data == "aGVsbG8="


def test_tool_result_to_mcp_content_parses_json_string_image_blocks() -> None:
    content = '[{"type":"image","source":{"type":"base64","data":"aGVsbG8=","media_type":"image/png"}}]'
    result = tool_result_to_mcp_content(content)
    assert len(result) == 1
    assert result[0].type == "image"
    assert result[0].mimeType == "image/png"


def test_tool_result_to_mcp_content_parses_codex_input_image_blocks() -> None:
    content = '[{"type":"input_image","image_url":"data:image/png;base64,aGVsbG8="}]'
    result = tool_result_to_mcp_content(content)
    assert len(result) == 1
    assert result[0].type == "image"
    assert result[0].mimeType == "image/png"
    assert result[0].data == "aGVsbG8="


def test_truncate_long_text_keeps_prefix_and_suffix() -> None:
    text = "a" * (MAX_TEXT_LENGTH + 20)
    truncated = truncate_long_text(text)
    assert "<|truncate_long_text|>" in truncated
    assert str(MAX_TEXT_LENGTH + 20) in truncated
    assert len(truncated) > MAX_TEXT_LENGTH


def test_tool_result_to_mcp_content_truncates_text_chunk() -> None:
    content = tool_result_to_mcp_content("a" * (MAX_TEXT_LENGTH + 20))
    assert len(content) == 1
    assert content[0].type == "text"
    assert "<|truncate_long_text|>" in content[0].text


def test_anthropic_non_stream_followup_returns_fast_ok() -> None:
    app = create_app(port=19399)
    payload = {
        "model": "claude-sonnet-4-20250514",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "hi",
                    }
                ],
            }
        ],
        "tools": [{"name": "Bash", "description": "Run shell", "input_schema": {"type": "object"}}],
    }
    with TestClient(app) as client:
        response = client.post(
            "/harness_to_mcp/v1/messages",
            headers={"x-api-key": "token-1"},
            json=payload,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "ok"
