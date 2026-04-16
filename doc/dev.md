# Develop Document

## Flow

1. MCP `initialize` creates a streamable HTTP session.
2. In plain server mode, the server only waits for a harness request to connect through one hijack API.
3. In helper mode, the subcommand starts a colocated server plus one harness process, and pins that harness to the first MCP session id.
4. The harness sends a request to one hijack API:
   - OpenAI chat completions
   - OpenAI responses
   - Anthropic messages
5. The hijack API extracts the tool list from that request and exposes it through MCP `tools/list`.
6. MCP `tools/call` resolves the waiting hijack request with a tool call payload for the matching wire API.
7. The harness executes the tool, appends the tool result, and sends a new LLM API request.
8. The hijack API matches the tool call id, completes the pending MCP call, and keeps the new harness request open for the next MCP tool call.

## Config safety

- current local harness configs are not modified by helper launch commands
- use temporary config roots for `opencode`, `codex`, and `claude`
- keep manual backups under `/tmp/harness-configs-bak` when testing against real local installs

## Debug tips

- `harness_to_mcp --port 9330`
- `harness_to_mcp codex --port 9330`
- `harness_to_mcp claude --port 9330`
- `curl http://127.0.0.1:9330/harness_to_mcp/health`
