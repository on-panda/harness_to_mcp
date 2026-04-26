from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
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
    tool_calls: list[ToolCallSpec] | None = None


@dataclass(slots=True)
class InitialPrompts:
    instructions: str | None = None
    user_prompt: str | None = None
    harness_context: str | None = None


@dataclass(slots=True)
class HijackRequest:
    model: str
    stream: bool
    tools: list[types.Tool]
    tool_results: list[ToolResult]
    initial_prompts: InitialPrompts | None = None
    initial_request: dict[str, Any] | None = None
    unsupported_tools: list[dict[str, Any]] = field(default_factory=list)


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
        raw_tools = body.get("tools") or []
        return HijackRequest(
            model=body.get("model") or HIJACK_MODEL_ID,
            stream=bool(body.get("stream")),
            tools=_extract_openai_function_tools(raw_tools),
            tool_results=_extract_chat_tool_results(body.get("messages") or []),
            initial_prompts=_extract_openai_chat_initial_prompts(body),
            initial_request=body,
            unsupported_tools=_extract_openai_unsupported_tools(raw_tools),
        )

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        created = int(time.time())
        message: dict[str, Any] = {"role": "assistant", "content": payload.text or ""}
        finish_reason = "stop"
        tool_calls = _payload_tool_calls(payload)
        if tool_calls:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [_openai_chat_tool_call_message(tool_call) for tool_call in tool_calls],
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
        tool_calls = _payload_tool_calls(payload)
        if tool_calls:
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
                                "tool_calls": [
                                    _openai_chat_tool_call_chunk(tool_call, index)
                                    for index, tool_call in enumerate(tool_calls)
                                ],
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
        return _extract_bearer_token(headers.get("authorization")) or headers.get("session_id")

    def parse_request(self, body: dict[str, Any]) -> HijackRequest:
        raw_tools = body.get("tools") or []
        return HijackRequest(
            model=body.get("model") or HIJACK_MODEL_ID,
            stream=bool(body.get("stream")),
            tools=_extract_openai_function_tools(raw_tools),
            tool_results=_extract_responses_tool_results(body.get("input") or []),
            initial_prompts=_extract_responses_initial_prompts(body),
            initial_request=body,
            unsupported_tools=_extract_openai_unsupported_tools(raw_tools),
        )

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        response_id = _response_id("resp-harness-to-mcp")
        created = int(time.time())
        output = [_responses_message_item(payload.text or "")]
        output_text = payload.text or ""
        tool_calls = _payload_tool_calls(payload)
        if tool_calls:
            output = [_responses_function_call_item(tool_call) for tool_call in tool_calls]
            output_text = ""
        return {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": payload.model,
            "output": output,
            "output_text": output_text,
            "parallel_tool_calls": len(tool_calls) > 1,
        }

    def build_stream_events(self, payload: TurnPayload) -> list[dict[str, Any]]:
        response_id = _response_id("resp-harness-to-mcp")
        created = int(time.time())
        tool_calls = _payload_tool_calls(payload)
        if tool_calls:
            items = [_responses_function_call_item(tool_call) for tool_call in tool_calls]
            parallel_tool_calls = len(tool_calls) > 1
            events = [
                {
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "in_progress",
                        "model": payload.model,
                        "output": [],
                        "parallel_tool_calls": parallel_tool_calls,
                    },
                },
                {
                    "type": "response.in_progress",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "in_progress",
                        "model": payload.model,
                        "output": [],
                        "parallel_tool_calls": parallel_tool_calls,
                    },
                },
            ]
            for output_index, item in enumerate(items):
                events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": {
                                "id": item["id"],
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": item["call_id"],
                                "name": item["name"],
                                "arguments": "",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "response_id": response_id,
                            "item_id": item["id"],
                            "output_index": output_index,
                            "delta": item["arguments"],
                        },
                        {
                            "type": "response.function_call_arguments.done",
                            "response_id": response_id,
                            "item_id": item["id"],
                            "output_index": output_index,
                            "call_id": item["call_id"],
                            "name": item["name"],
                            "arguments": item["arguments"],
                        },
                        {
                            "type": "response.output_item.done",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": item,
                        },
                    ]
                )
            events.append(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "status": "completed",
                        "model": payload.model,
                        "output": items,
                        "output_text": "",
                        "parallel_tool_calls": parallel_tool_calls,
                    },
                }
            )
            return _with_sequence_numbers(events)
        item = _responses_message_item(payload.text or "")
        return _with_sequence_numbers([
            {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "status": "in_progress",
                    "model": payload.model,
                    "output": [],
                    "parallel_tool_calls": False,
                },
            },
            {
                "type": "response.in_progress",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "status": "in_progress",
                    "model": payload.model,
                    "output": [],
                    "parallel_tool_calls": False,
                },
            },
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
            {
                "type": "response.output_text.delta",
                "response_id": response_id,
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": payload.text or "",
            },
            {
                "type": "response.output_text.done",
                "response_id": response_id,
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "text": payload.text or "",
            },
            {
                "type": "response.output_item.done",
                "response_id": response_id,
                "output_index": 0,
                "item": item,
            },
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "status": "completed",
                    "model": payload.model,
                    "output": [item],
                    "output_text": payload.text or "",
                    "parallel_tool_calls": False,
                },
            },
        ])

    def build_stream_chunks(self, payload: TurnPayload) -> list[bytes]:
        return [
            *(_encode_event(event["type"], event) for event in self.build_stream_events(payload)),
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
        raw_tools = body.get("tools") or []
        return HijackRequest(
            model=body.get("model") or HIJACK_MODEL_ID,
            stream=bool(body.get("stream")),
            tools=_extract_anthropic_tools(raw_tools),
            tool_results=_extract_anthropic_tool_results(body.get("messages") or []),
            initial_prompts=_extract_anthropic_initial_prompts(body),
            initial_request=body,
            unsupported_tools=_extract_anthropic_unsupported_tools(raw_tools),
        )

    def build_json_response(self, payload: TurnPayload) -> dict[str, Any]:
        tool_calls = _payload_tool_calls(payload)
        if tool_calls:
            return {
                "id": _response_id("msg-harness-to-mcp"),
                "type": "message",
                "role": "assistant",
                "model": payload.model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_call.call_id,
                        "name": tool_call.name,
                        "input": tool_call.arguments,
                    }
                    for tool_call in tool_calls
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
        tool_calls = _payload_tool_calls(payload)
        if tool_calls:
            chunks = [
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
            ]
            for index, tool_call in enumerate(tool_calls):
                chunks.extend(
                    [
                        _encode_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tool_call.call_id,
                                    "name": tool_call.name,
                                    "input": {},
                                },
                            },
                        ),
                        _encode_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": _compact_json(tool_call.arguments),
                                },
                            },
                        ),
                        _encode_event("content_block_stop", {"type": "content_block_stop", "index": index}),
                    ]
                )
            chunks.extend(
                [
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
            )
            return chunks
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


def _extract_openai_chat_initial_prompts(body: dict[str, Any]) -> InitialPrompts | None:
    instructions = _join_blocks(
        message.get("content")
        for message in body.get("messages") or []
        if message.get("role") == "system" and isinstance(message.get("content"), str)
    )
    developer_messages = [
        _normalize_block(message.get("content"))
        for message in body.get("messages") or []
        if message.get("role") == "developer" and isinstance(message.get("content"), str)
    ]
    user_messages = [
        _sanitize_initial_prompt(message.get("content"), decode_json_string=True)
        for message in body.get("messages") or []
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]
    return _build_initial_prompts(
        instructions=instructions,
        harness_context=_join_blocks([*developer_messages, *user_messages[:-1]]),
        user_prompt=user_messages[-1] if user_messages else None,
    )


def _extract_responses_initial_prompts(body: dict[str, Any]) -> InitialPrompts | None:
    message_blocks = [
        (_join_blocks(_responses_text_fragments(item)), item.get("role"))
        for item in body.get("input") or []
        if item.get("type") == "message"
    ]
    user_messages = [text for text, role in message_blocks if role == "user"]
    developer_messages = [text for text, role in message_blocks if role == "developer"]
    return _build_initial_prompts(
        instructions=_join_blocks([body.get("instructions")]),
        harness_context=_join_blocks([*developer_messages, *user_messages[:-1]]),
        user_prompt=user_messages[-1] if user_messages else None,
    )


def _extract_anthropic_initial_prompts(body: dict[str, Any]) -> InitialPrompts | None:
    user_blocks = [
        content.get("text")
        for message in body.get("messages") or []
        if message.get("role") == "user"
        for content in message.get("content") or []
        if isinstance(content, dict) and isinstance(content.get("text"), str)
    ]
    return _build_initial_prompts(
        instructions=_join_blocks(
            item.get("text")
            for item in body.get("system") or []
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ),
        harness_context=_join_blocks(user_blocks[:-1]),
        user_prompt=user_blocks[-1] if user_blocks else None,
    )


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


def _extract_openai_unsupported_tools(raw_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        function = raw_tool.get("function") or raw_tool
        raw_name = function.get("name") or raw_tool.get("name")
        tool_type = raw_tool.get("type") or "unknown"
        if tool_type == "function" and raw_name:
            continue
        item = {"type": tool_type}
        if raw_name:
            item["name"] = raw_name
        result.append(item)
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


def _extract_anthropic_unsupported_tools(raw_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: raw_tool[key] for key in ("name", "type") if key in raw_tool}
        for raw_tool in raw_tools
        if not raw_tool.get("name")
    ]


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


def _with_sequence_numbers(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, event in enumerate(events, start=1):
        event["event_id"] = _response_id("event-harness-to-mcp")
        event["sequence_number"] = index
    return events


def _openai_chat_tool_call_message(tool_call: ToolCallSpec) -> dict[str, Any]:
    return {
        "id": tool_call.call_id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": _compact_json(tool_call.arguments)},
    }


def _openai_chat_tool_call_chunk(tool_call: ToolCallSpec, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "id": tool_call.call_id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": _compact_json(tool_call.arguments)},
    }


def _payload_tool_calls(payload: TurnPayload) -> list[ToolCallSpec]:
    return payload.tool_calls or []


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


def _build_initial_prompts(
    *,
    instructions: str | None = None,
    harness_context: str | None = None,
    user_prompt: str | None = None,
) -> InitialPrompts | None:
    instructions = _normalize_block(instructions)
    harness_context = _normalize_block(harness_context)
    user_prompt = _sanitize_initial_prompt(user_prompt)
    if instructions is None and harness_context is None and user_prompt is None:
        return None
    return InitialPrompts(instructions=instructions, harness_context=harness_context, user_prompt=user_prompt)


def _join_blocks(blocks: Any) -> str | None:
    parts = [text for text in (_normalize_block(block) for block in blocks) if text]
    if not parts:
        return None
    return "\n\n".join(parts)


def _normalize_block(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    return stripped or None


def _sanitize_initial_prompt(text: str | None, *, decode_json_string: bool = False) -> str | None:
    if not isinstance(text, str):
        return None
    if decode_json_string:
        candidate = text.strip()
        if candidate.startswith('"'):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(decoded, str):
                    text = decoded
    text = re.sub(r"<\|harness_to_mcp_start\|>.*?<\|harness_to_mcp_end\|>", "", text, flags=re.S)
    return _normalize_block(text)


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
