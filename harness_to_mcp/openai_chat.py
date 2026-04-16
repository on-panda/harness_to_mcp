from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from mcp import types

HIJACK_MODEL_ID = "harness_to_mcp_hijack_api"


@dataclass(slots=True)
class ToolCallSpec:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    tool_call_id: str
    content: str


@dataclass(slots=True)
class CompletionPayload:
    model: str
    text: str | None = None
    tool_call: ToolCallSpec | None = None


def extract_tools(body: dict[str, Any]) -> list[types.Tool]:
    result: list[types.Tool] = []
    for raw_tool in body.get("tools") or []:
        if raw_tool.get("type") != "function":
            continue
        function = raw_tool.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        schema = function.get("parameters") or {"type": "object", "properties": {}}
        result.append(
            types.Tool(
                name=name,
                description=function.get("description") or "",
                inputSchema=schema,
            )
        )
    return result


def extract_tool_results(body: dict[str, Any]) -> list[ToolResult]:
    result: list[ToolResult] = []
    for message in body.get("messages") or []:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not tool_call_id:
            continue
        result.append(
            ToolResult(
                tool_call_id=tool_call_id,
                content=_normalize_message_content(message.get("content")),
            )
        )
    return result


def request_has_tools(body: dict[str, Any]) -> bool:
    return bool(body.get("tools"))


def default_text_response(body: dict[str, Any]) -> str:
    messages = body.get("messages") or []
    joined = "\n".join(
        part.get("content", "")
        for part in messages
        if isinstance(part, dict) and isinstance(part.get("content"), str)
    )
    if "You are a title generator." in joined or "Generate a title for this conversation:" in joined:
        return "Harness session"
    return "ok"


def build_json_response(payload: CompletionPayload) -> dict[str, Any]:
    response_id = _response_id()
    created = int(time.time())
    message: dict[str, Any] = {"role": "assistant", "content": payload.text or ""}
    finish_reason = "stop"
    if payload.tool_call is not None:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [_tool_call_message(payload.tool_call)],
        }
        finish_reason = "tool_calls"
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": payload.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def build_stream_chunks(payload: CompletionPayload) -> list[bytes]:
    response_id = _response_id()
    created = int(time.time())
    if payload.tool_call is not None:
        return [
            _encode_sse(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [_tool_call_stream_chunk(payload.tool_call)],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            _encode_sse(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": payload.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            ),
            b"data: [DONE]\n\n",
        ]
    return [
        _encode_sse(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": payload.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": payload.text or ""},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _encode_sse(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": payload.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        ),
        b"data: [DONE]\n\n",
    ]


def build_stream_heartbeat(model: str) -> bytes:
    return _encode_sse(
        {
            "id": _response_id("chatcmpl-harness-to-mcp-heartbeat"),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
        }
    )


def openai_error(message: str, error_type: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type}}


def _normalize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _tool_call_message(tool_call: ToolCallSpec) -> dict[str, Any]:
    return {
        "id": tool_call.call_id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _tool_call_stream_chunk(tool_call: ToolCallSpec) -> dict[str, Any]:
    return {
        "index": 0,
        "id": tool_call.call_id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _encode_sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _response_id(prefix: str = "chatcmpl-harness-to-mcp") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
