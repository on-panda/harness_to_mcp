# harness_to_mcp

[中文版本说明](./README_cn.md)

`harness_to_mcp` exposes harness internal tools as an MCP HTTP server by hijacking LLM API traffic.

## What it does

- starts one MCP HTTP server and one hijack API server on the same port
- starts one harness process per MCP session
- extracts the harness tool list from intercepted LLM requests
- forwards MCP `tools/call` into the harness tool loop and maps the tool result back to MCP
- stops the harness process when the MCP session is closed

## Supported harnesses

- `opencode` via OpenAI chat completions
- `codex` via OpenAI responses API
- `claude` via Anthropic messages API

## Exposed endpoints

Default port: `9330`

- `POST /mcp`
- `POST /harness_to_mcp/mcp`
- `GET /harness_to_mcp/v1/models`
- `POST /harness_to_mcp/v1/chat/completions`
- `POST /harness_to_mcp/v1/responses`
- `POST /harness_to_mcp/v1/messages`

The two MCP paths are equivalent.

## Install

```bash
pip install harness_to_mcp
```

## Run the server

```bash
harness_to_mcp --port 9330
```

This mode starts only the server. It listens on MCP plus all hijack API routes, but does not launch any harness by itself.

## Launch a harness directly

```bash
harness_to_mcp opencode --port 9330
harness_to_mcp codex --port 9330
harness_to_mcp claude --port 9330
```

Each helper command starts its own colocated server and one harness instance together. If the harness exits later, the server process keeps running.

The helper commands do not overwrite your current harness configs. A backup copy can be kept at `/tmp/harness-configs-bak` before local testing.

## Python API

```python
from harness_to_mcp import HarnessToMcp

with HarnessToMcp(port=9330) as server:
    print(server.mcp_url)
    print(server.hijack_base_url)
    print(server.anthropic_base_url)
```

## Notes

- the LLM API layer is split into reusable adapters for chat completions, responses, and messages
- the harness layer is split into reusable launchers for `opencode`, `codex`, and `claude`
- plain server mode never auto-launches a harness
- intercepted waiting requests stay alive with periodic heartbeat bytes while MCP is deciding the next tool call
- if the harness does not reconnect to the hijack API within 30 seconds, MCP requests fail with a hijack-not-connected error
