import json

from harness_to_mcp.adapters import (
    MAX_TEXT_LENGTH,
    AnthropicMessagesAdapter,
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


def test_responses_adapter_builds_function_call_stream() -> None:
    adapter = OpenAIResponsesAdapter()
    payload = TurnPayload(
        model="demo-model",
        tool_call=ToolCallSpec(call_id="call_1", name="exec_command", arguments={"cmd": "printf hi"}),
    )
    chunks = adapter.build_stream_chunks(payload)
    assert chunks[-1] == b"data: [DONE]\n\n"
    combined = b"".join(chunks).decode("utf-8")
    assert "response.function_call_arguments.done" in combined
    assert '"call_id": "call_1"' in combined


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


def test_anthropic_adapter_builds_tool_use_json() -> None:
    adapter = AnthropicMessagesAdapter()
    payload = TurnPayload(
        model="claude-sonnet-4-20250514",
        tool_call=ToolCallSpec(call_id="toolu_1", name="Bash", arguments={"cmd": "printf hi"}),
    )
    response = adapter.build_json_response(payload)
    assert response["stop_reason"] == "tool_use"
    assert response["content"][0]["type"] == "tool_use"
    assert response["content"][0]["input"]["cmd"] == "printf hi"


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
