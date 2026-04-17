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
        tool_calls=[ToolCallSpec(call_id="call_1", name="glob", arguments={"pattern": "*.md"})],
    )
    response = build_json_response(payload)
    tool_call = response["choices"][0]["message"]["tool_calls"][0]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert tool_call["id"] == "call_1"
    assert tool_call["function"]["arguments"] == '{"pattern":"*.md"}'


def test_build_stream_chunks_for_batched_tool_calls() -> None:
    payload = CompletionPayload(
        model="demo-model",
        tool_calls=[
            ToolCallSpec(
                call_id="toolu_01Taz9dKi5vJBtP8G6yvA2Nj",
                name="bash",
                arguments={"command": "pwd", "description": "显示当前工作目录"},
            ),
            ToolCallSpec(
                call_id="toolu_01CddNtk7WZRghZftY1hCuGp",
                name="bash",
                arguments={"command": "git status", "description": "查看 git 仓库状态"},
            ),
            ToolCallSpec(
                call_id="toolu_01UsQXM8FiQ9f3J59XPLisHV",
                name="bash",
                arguments={"command": "git branch -a", "description": "查看所有分支"},
            ),
            ToolCallSpec(
                call_id="toolu_01MvMvNjGs99QsbTeBr7FrM7",
                name="bash",
                arguments={"command": "git log --oneline -10", "description": "查看最近10条提交记录"},
            ),
            ToolCallSpec(
                call_id="toolu_01DNhMSJMiMiAPRmfBxRAPn9",
                name="bash",
                arguments={"command": "ls -la", "description": "列出目录内容"},
            ),
        ],
    )
    chunks = build_stream_chunks(payload)
    first = json.loads(chunks[0].decode().removeprefix("data: ").strip())
    tool_calls = first["choices"][0]["delta"]["tool_calls"]
    assert len(tool_calls) == 5
    assert tool_calls[0] == {
        "index": 0,
        "id": "toolu_01Taz9dKi5vJBtP8G6yvA2Nj",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": '{"command":"pwd","description":"显示当前工作目录"}',
        },
    }
    assert tool_calls[-1] == {
        "index": 4,
        "id": "toolu_01DNhMSJMiMiAPRmfBxRAPn9",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": '{"command":"ls -la","description":"列出目录内容"}',
        },
    }


def test_build_stream_chunks_end_with_done() -> None:
    payload = CompletionPayload(model="demo-model", text="ok")
    chunks = build_stream_chunks(payload)
    assert chunks[-1] == b"data: [DONE]\n\n"
    first = json.loads(chunks[0].decode().removeprefix("data: ").strip())
    assert first["choices"][0]["delta"]["content"] == "ok"
