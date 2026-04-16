# Develop Document

## Flow

1. MCP `initialize` creates a streamable HTTP session and starts one harness process.
2. The harness sends a chat completions request to `/harness_to_mcp/v1/chat/completions`.
3. The hijack API extracts `tools` from that request and exposes them through MCP `tools/list`.
4. MCP `tools/call` resolves the waiting hijack request with an OpenAI tool call response.
5. The harness executes the tool, appends the tool message, and sends a new chat completions request.
6. The hijack API matches the tool call id, completes the pending MCP call, and keeps the new harness request open for the next MCP tool call.

## Debug tips

- `harness_to_mcp opencode --port 9330`
- inspect the temporary opencode log path printed by your own wrapper if you add local debugging
- use `opencode debug config` to verify `XDG_CONFIG_HOME` based config loading
