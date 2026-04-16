import json

from harness_to_mcp.openai_chat import (
    CompletionPayload,
    ToolCallSpec,
    build_json_response,
    build_stream_chunks,
    extract_tool_results,
    extract_tools,
)


def test_extract_tools_and_results() -> None:
    body = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "glob",
                    "description": "List files",
                    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                },
            }
        ],
        "messages": [
            {"role": "tool", "tool_call_id": "call_1", "content": "README.md"},
        ],
    }
    tools = extract_tools(body)
    results = extract_tool_results(body)
    assert [tool.name for tool in tools] == ["glob"]
    assert tools[0].inputSchema["properties"]["pattern"]["type"] == "string"
    assert results[0].tool_call_id == "call_1"
    assert results[0].content == "README.md"


def test_build_json_response_for_tool_call() -> None:
    payload = CompletionPayload(
        model="demo-model",
        tool_call=ToolCallSpec(call_id="call_1", name="glob", arguments={"pattern": "*.md"}),
    )
    response = build_json_response(payload)
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert tool_call["id"] == "call_1"
    assert tool_call["function"]["arguments"] == '{"pattern":"*.md"}'


def test_build_stream_chunks_end_with_done() -> None:
    payload = CompletionPayload(model="demo-model", text="ok")
    chunks = build_stream_chunks(payload)
    assert chunks[-1] == b"data: [DONE]\n\n"
    first = json.loads(chunks[0].decode().removeprefix("data: ").strip())
    assert first["choices"][0]["delta"]["content"] == "ok"
