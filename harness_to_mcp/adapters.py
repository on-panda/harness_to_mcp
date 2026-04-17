from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from mcp import types

HIJACK_MODEL_ID = "harness_to_mcp_hijack_api"
MAX_TEXT_LENGTH = 256 * 1024


@dataclass(slots=True)
class ToolCallSpec:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    tool_call_id: str
    content: Any


@dataclass(slots=True)
class TurnPayload:
    model: str
    text: str | None = None
    tool_call: ToolCallSpec | None = None


@dataclass(slots=True)
class HijackRequest:
    model: str
    stream: bool
    tools: list[types.Tool]
    tool_results: list[ToolResult]


class ApiAdapter:
    name = ""
    route_path = ""

    def session_token_from_headers(self, headers: Any) -> str | None:
        raise NotImplementedError

    def parse_request(self, body: dict[str, Any]) -> HijackRequest:
        raise NotImplementedError

    def request_has_tools(self, body: dict[str, Any]) -> bool:
        return bool(body.get("tools"))

    def default_text_response(self, body: dict[str, Any]) -> str:
        return "Harness session" if _looks_like_title_request(self._text_fragments(body)) else "ok"

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        raise NotImplementedError

    def build_stream_chunks(self, payload: TurnPayload) -> list[bytes]:
        raise NotImplementedError

    def build_stream_heartbeat(self, model: str) -> bytes:
        return b": ping\n\n"

    def error_body(self, message: str) -> dict[str, Any]:
        raise NotImplementedError

    def _text_fragments(self, body: dict[str, Any]) -> list[str]:
        return []


class OpenAIChatAdapter(ApiAdapter):
    name = "openai_chat"
    route_path = "/harness_to_mcp/v1/chat/completions"

    def session_token_from_headers(self, headers: Any) -> str | None:
        return _extract_bearer_token(headers.get("authorization"))

    def parse_request(self, body: dict[str, Any]) -> HijackRequest:
        return HijackRequest(
            model=body.get("model") or HIJACK_MODEL_ID,
            stream=bool(body.get("stream")),
            tools=_extract_openai_function_tools(body.get("tools") or []),
            tool_results=_extract_chat_tool_results(body.get("messages") or []),
        )

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        created = int(time.time())
        message: dict[str, Any] = {"role": "assistant", "content": payload.text or ""}
        finish_reason = "stop"
        if payload.tool_call is not None:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [_openai_chat_tool_call_message(payload.tool_call)],
            }
            finish_reason = "tool_calls"
        return {
            "id": _response_id("chatcmpl-harness-to-mcp"),
            "object": "chat.completion",
            "created": created,
            "model": payload.model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def build_stream_chunks(self, payload: TurnPayload) -> list[bytes]:
        response_id = _response_id("chatcmpl-harness-to-mcp")
        created = int(time.time())
        if payload.tool_call is not None:
            return [
                _encode_data(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload.model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [_openai_chat_tool_call_chunk(payload.tool_call)],
                            },
                            "finish_reason": None,
                        }],
                    }
                ),
                _encode_data(
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
            _encode_data(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": payload.model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": payload.text or ""},
                        "finish_reason": None,
                    }],
                }
            ),
            _encode_data(
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

    def build_stream_heartbeat(self, model: str) -> bytes:
        return _encode_data(
            {
                "id": _response_id("chatcmpl-harness-to-mcp-heartbeat"),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
            }
        )

    def error_body(self, message: str) -> dict[str, Any]:
        return {"error": {"message": message, "type": "invalid_request_error"}}

    def _text_fragments(self, body: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                result.append(content)
        return result


class OpenAIResponsesAdapter(ApiAdapter):
    name = "openai_responses"
    route_path = "/harness_to_mcp/v1/responses"

    def session_token_from_headers(self, headers: Any) -> str | None:
        return _extract_bearer_token(headers.get("authorization"))

    def parse_request(self, body: dict[str, Any]) -> HijackRequest:
        return HijackRequest(
            model=body.get("model") or HIJACK_MODEL_ID,
            stream=bool(body.get("stream")),
            tools=_extract_openai_function_tools(body.get("tools") or []),
            tool_results=_extract_responses_tool_results(body.get("input") or []),
        )

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        response_id = _response_id("resp-harness-to-mcp")
        created = int(time.time())
        output = [_responses_message_item(payload.text or "")]
        output_text = payload.text or ""
        if payload.tool_call is not None:
            output = [_responses_function_call_item(payload.tool_call)]
            output_text = ""
        return {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": payload.model,
            "output": output,
            "output_text": output_text,
            "parallel_tool_calls": False,
        }

    def build_stream_chunks(self, payload: TurnPayload) -> list[bytes]:
        response_id = _response_id("resp-harness-to-mcp")
        created = int(time.time())
        if payload.tool_call is not None:
            item = _responses_function_call_item(payload.tool_call)
            return [
                _encode_event(
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created,
                            "status": "in_progress",
                            "model": payload.model,
                            "output": [],
                        },
                    },
                ),
                _encode_event(
                    "response.in_progress",
                    {
                        "type": "response.in_progress",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created,
                            "status": "in_progress",
                            "model": payload.model,
                            "output": [],
                        },
                    },
                ),
                _encode_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": 0,
                        "item": {
                            "id": item["id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": item["call_id"],
                            "name": item["name"],
                            "arguments": "",
                        },
                    },
                ),
                _encode_event(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "delta": item["arguments"],
                    },
                ),
                _encode_event(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "output_index": 0,
                        "arguments": item["arguments"],
                    },
                ),
                _encode_event(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": 0,
                        "item": item,
                    },
                ),
                _encode_event(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created,
                            "status": "completed",
                            "model": payload.model,
                            "output": [item],
                        },
                    },
                ),
                b"data: [DONE]\n\n",
            ]
        item = _responses_message_item(payload.text or "")
        return [
            _encode_event(
                "response.created",
                {
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "in_progress",
                        "model": payload.model,
                        "output": [],
                    },
                },
            ),
            _encode_event(
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "in_progress",
                        "model": payload.model,
                        "output": [],
                    },
                },
            ),
            _encode_event(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": 0,
                    "item": {
                        "id": item["id"],
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            ),
            _encode_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": payload.text or "",
                },
            ),
            _encode_event(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "text": payload.text or "",
                },
            ),
            _encode_event(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": 0,
                    "item": item,
                },
            ),
            _encode_event(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "completed",
                        "model": payload.model,
                        "output": [item],
                    },
                },
            ),
            b"data: [DONE]\n\n",
        ]

    def error_body(self, message: str) -> dict[str, Any]:
        return {"error": {"message": message, "type": "invalid_request_error"}}

    def _text_fragments(self, body: dict[str, Any]) -> list[str]:
        result: list[str] = []
        instructions = body.get("instructions")
        if isinstance(instructions, str):
            result.append(instructions)
        for item in body.get("input") or []:
            result.extend(_responses_text_fragments(item))
        return result


class AnthropicMessagesAdapter(ApiAdapter):
    name = "anthropic_messages"
    route_path = "/harness_to_mcp/v1/messages"

    def session_token_from_headers(self, headers: Any) -> str | None:
        api_key = headers.get("x-api-key")
        if api_key:
            return api_key
        return _extract_bearer_token(headers.get("authorization"))

    def parse_request(self, body: dict[str, Any]) -> HijackRequest:
        return HijackRequest(
            model=body.get("model") or HIJACK_MODEL_ID,
            stream=bool(body.get("stream")),
            tools=_extract_anthropic_tools(body.get("tools") or []),
            tool_results=_extract_anthropic_tool_results(body.get("messages") or []),
        )

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        if payload.tool_call is not None:
            return {
                "id": _response_id("msg-harness-to-mcp"),
                "type": "message",
                "role": "assistant",
                "model": payload.model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": payload.tool_call.call_id,
                        "name": payload.tool_call.name,
                        "input": payload.tool_call.arguments,
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        return {
            "id": _response_id("msg-harness-to-mcp"),
            "type": "message",
            "role": "assistant",
            "model": payload.model,
            "content": [{"type": "text", "text": payload.text or ""}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    def build_stream_chunks(self, payload: TurnPayload) -> list[bytes]:
        message_id = _response_id("msg-harness-to-mcp")
        if payload.tool_call is not None:
            return [
                _encode_event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": message_id,
                            "type": "message",
                            "role": "assistant",
                            "model": payload.model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                ),
                _encode_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": payload.tool_call.call_id,
                            "name": payload.tool_call.name,
                            "input": {},
                        },
                    },
                ),
                _encode_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": _compact_json(payload.tool_call.arguments),
                        },
                    },
                ),
                _encode_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
                _encode_event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 0},
                    },
                ),
                _encode_event("message_stop", {"type": "message_stop"}),
            ]
        return [
            _encode_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": payload.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            ),
            _encode_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _encode_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": payload.text or ""},
                },
            ),
            _encode_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _encode_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            ),
            _encode_event("message_stop", {"type": "message_stop"}),
        ]

    def build_stream_heartbeat(self, model: str) -> bytes:
        return _encode_event("ping", {"type": "ping"})

    def error_body(self, message: str) -> dict[str, Any]:
        return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}

    def _text_fragments(self, body: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for item in body.get("system") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                result.append(item["text"])
        for message in body.get("messages") or []:
            for content in message.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    result.append(content["text"])
        return result


def build_adapters() -> dict[str, ApiAdapter]:
    adapters = [OpenAIChatAdapter(), OpenAIResponsesAdapter(), AnthropicMessagesAdapter()]
    return {adapter.name: adapter for adapter in adapters}


def adapter_routes(adapters: dict[str, ApiAdapter]) -> dict[str, ApiAdapter]:
    return {adapter.route_path: adapter for adapter in adapters.values()}


def _extract_openai_function_tools(raw_tools: list[dict[str, Any]]) -> list[types.Tool]:
    result: list[types.Tool] = []
    for raw_tool in raw_tools:
        if raw_tool.get("type") != "function":
            continue
        function = raw_tool.get("function") or raw_tool
        name = function.get("name") or raw_tool.get("name")
        if not name:
            continue
        parameters = function.get("parameters") or raw_tool.get("parameters") or {"type": "object", "properties": {}}
        result.append(types.Tool(name=name, description=function.get("description") or raw_tool.get("description") or "", inputSchema=parameters))
    return result


def _extract_anthropic_tools(raw_tools: list[dict[str, Any]]) -> list[types.Tool]:
    result: list[types.Tool] = []
    for raw_tool in raw_tools:
        name = raw_tool.get("name")
        if not name:
            continue
        schema = raw_tool.get("input_schema") or {"type": "object", "properties": {}}
        result.append(types.Tool(name=name, description=raw_tool.get("description") or "", inputSchema=schema))
    return result


def _extract_chat_tool_results(messages: list[dict[str, Any]]) -> list[ToolResult]:
    result: list[ToolResult] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            result.append(ToolResult(tool_call_id=tool_call_id, content=message.get("content")))
    return result


def _extract_responses_tool_results(items: list[dict[str, Any]]) -> list[ToolResult]:
    result: list[ToolResult] = []
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        call_id = item.get("call_id")
        if call_id:
            result.append(ToolResult(tool_call_id=call_id, content=item.get("output")))
    return result


def _extract_anthropic_tool_results(messages: list[dict[str, Any]]) -> list[ToolResult]:
    result: list[ToolResult] = []
    for message in messages:
        for item in message.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "tool_result":
                continue
            tool_use_id = item.get("tool_use_id")
            if tool_use_id:
                result.append(ToolResult(tool_call_id=tool_use_id, content=item.get("content")))
    return result


def _responses_text_fragments(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    if item.get("type") == "message":
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                result.append(content["text"])
    elif item.get("type") == "function_call_output":
        result.append(_normalize_content(item.get("output")))
    elif isinstance(item.get("content"), str):
        result.append(item["content"])
    return result


def _responses_message_item(text: str) -> dict[str, Any]:
    return {
        "id": _response_id("msg-harness-to-mcp"),
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _responses_function_call_item(tool_call: ToolCallSpec) -> dict[str, Any]:
    return {
        "id": _response_id("fc-harness-to-mcp"),
        "type": "function_call",
        "status": "completed",
        "call_id": tool_call.call_id,
        "name": tool_call.name,
        "arguments": _compact_json(tool_call.arguments),
    }


def _openai_chat_tool_call_message(tool_call: ToolCallSpec) -> dict[str, Any]:
    return {
        "id": tool_call.call_id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": _compact_json(tool_call.arguments)},
    }


def _openai_chat_tool_call_chunk(tool_call: ToolCallSpec) -> dict[str, Any]:
    return {
        "index": 0,
        "id": tool_call.call_id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": _compact_json(tool_call.arguments)},
    }


def _looks_like_title_request(fragments: list[str]) -> bool:
    joined = "\n".join(fragment for fragment in fragments if fragment)
    return "You are a title generator." in joined or "Generate a title for this conversation:" in joined


def _normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def truncate_long_text(text: str, max_text_length: int = MAX_TEXT_LENGTH) -> str:
    if len(text) <= max_text_length:
        return text
    prefix_length = (max_text_length + 1) // 2
    suffix_length = max_text_length - prefix_length
    omitted_length = len(text) - max_text_length
    return (
        f"(Total length: {len(text)} characters. Showing {max_text_length} characters after truncating the middle.)\n"
        f"{text[:prefix_length]}\n"
        "<|truncate_long_text|>\n"
        f"{{Output exceeded {max_text_length} characters. Omitted {omitted_length} characters from the middle.}} ......\n"
        "<|truncate_long_text|>\n"
        f"{text[len(text) - suffix_length:]}"
    )


def tool_result_to_mcp_content(content: Any) -> list[types.TextContent | types.ImageContent]:
    parsed = _maybe_parse_json_string(content)
    if isinstance(parsed, list):
        blocks = [_convert_content_item(item) for item in parsed]
        if all(block is not None for block in blocks):
            return [block for block in blocks if block is not None]
    block = _convert_content_item(parsed)
    if block is not None:
        return [block]
    return [types.TextContent(type="text", text=truncate_long_text(_normalize_content(content)))]


def _convert_content_item(item: Any) -> types.TextContent | types.ImageContent | None:
    if isinstance(item, str):
        return types.TextContent(type="text", text=truncate_long_text(item))
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type in {"text", "output_text"} and isinstance(item.get("text"), str):
        return types.TextContent(type="text", text=truncate_long_text(item["text"]))
    if item_type == "image":
        if isinstance(item.get("mimeType"), str) and isinstance(item.get("data"), str):
            return types.ImageContent(type="image", mimeType=item["mimeType"], data=item["data"])
        source = item.get("source")
        if isinstance(source, dict) and source.get("type") == "base64" and isinstance(source.get("data"), str):
            mime_type = source.get("media_type") or source.get("mimeType")
            if isinstance(mime_type, str):
                return types.ImageContent(type="image", mimeType=mime_type, data=source["data"])
    if item_type == "input_image" and isinstance(item.get("image_url"), str):
        header, sep, data = item["image_url"].partition(",")
        if sep and header.startswith("data:") and header.endswith(";base64"):
            mime_type = header[5:-7]
            if mime_type:
                return types.ImageContent(type="image", mimeType=mime_type, data=data)
    return None


def _maybe_parse_json_string(content: Any) -> Any:
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text or text[0] not in "[{":
        return content
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return content


def _extract_bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _encode_data(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _encode_event(name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _response_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
