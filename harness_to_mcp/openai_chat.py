from .adapters import (
    HIJACK_MODEL_ID,
    OpenAIChatAdapter,
    ToolCallSpec,
    ToolResult,
    TurnPayload as CompletionPayload,
)

_adapter = OpenAIChatAdapter()

extract_tools = lambda body: _adapter.parse_request({"tools": body.get("tools") or []}).tools
extract_tool_results = lambda body: _adapter.parse_request({"messages": body.get("messages") or []}).tool_results
request_has_tools = _adapter.request_has_tools
default_text_response = _adapter.default_text_response
build_json_response = _adapter.build_json_response
build_stream_chunks = _adapter.build_stream_chunks
build_stream_heartbeat = _adapter.build_stream_heartbeat
openai_error = _adapter.error_body

__all__ = [
    "CompletionPayload",
    "HIJACK_MODEL_ID",
    "ToolCallSpec",
    "ToolResult",
    "build_json_response",
    "build_stream_chunks",
    "build_stream_heartbeat",
    "default_text_response",
    "extract_tool_results",
    "extract_tools",
    "openai_error",
    "request_has_tools",
]
