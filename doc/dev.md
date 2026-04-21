# Develop Document

## Flow

1. MCP `initialize` creates a streamable HTTP session.
2. In plain server mode, the server only waits for a harness request to connect through one hijack API.
3. In helper mode, the subcommand starts a colocated server plus one harness process, and pins that harness to the first MCP session id.
4. The harness sends a request to one hijack API transport:
   - OpenAI chat completions
   - OpenAI responses (`POST` or `WebSocket`)
   - Anthropic messages
5. The hijack API extracts the tool list from that request and exposes it through MCP `tools/list`.
6. MCP `tools/call` resolves the waiting hijack request with a tool call payload for the matching wire API.
7. The harness executes the tool, appends the tool result, and sends a new LLM API request.
8. The hijack API matches the tool call id, completes the pending MCP call, and keeps the new harness request open for the next MCP tool call.

## Initialize metadata

- the first tool-bearing hijack request is cached as the session bootstrap request
- `initialize.result.instructions` mirrors the captured harness bootstrap prompts after harness-specific normalization
- `initialize.result.capabilities.experimental.harness_to_mcp.initial_request` stores the original JSON body of that first tool-bearing hijack request
- `initialize.result.capabilities.experimental.harness_to_mcp.harness_info` stores harness metadata; when the harness name is known, `harness_info.harness` is set to that name
- in helper mode, `initialize` waits for that bootstrap request; in plain server mode this field can stay empty until an external harness connects
- in plain server mode, `harness_info` can stay empty when the connected wire protocol does not identify one harness unambiguously

## Instructions templates

- `codex` (`openai_responses`): keep top-level `instructions` verbatim, move developer messages plus non-final user messages into `<codex_harness_context>`, and treat the final user message as `<codex_initial_user_prompt>`
- `claude` (`anthropic_messages`): join `system[*].text` as the base instructions, keep earlier user text blocks such as reminder/context blocks in `<claude_harness_context>`, and use the last user text block as `<claude_initial_user_prompt>`
- `opencode` / `openclaw` (`openai_chat`): join system messages as the base instructions, keep developer messages plus non-final user messages in `<opencode_harness_context>` / `<openclaw_harness_context>`, and use the final user message as the initial user prompt
- bootstrap markers like `<|harness_to_mcp_start|> ... <|harness_to_mcp_end|>` are stripped from the derived initial user prompt so helper launch prompts do not leak into the MCP-facing instructions text

## Config safety

- current local harness configs are not modified by helper launch commands
- use temporary config roots for `opencode`, `openclaw`, `codex`, and `claude`
- `openclaw` helper mode uses one shared isolated gateway sidecar per `harness_to_mcp openclaw` process, not one gateway per MCP session

## Debug tips

- `harness_to_mcp --port 9330`
- `harness_to_mcp openclaw --port 9330`
- `harness_to_mcp codex --port 9330`
- `harness_to_mcp claude --port 9330`
- `curl http://127.0.0.1:9330/harness_to_mcp/health`
