# harness_to_mcp

`harness_to_mcp` exposes harness internal tools as an MCP HTTP server by hijacking an OpenAI-compatible chat completions endpoint.

## What it does

- starts one MCP HTTP server and one hijack OpenAI-compatible API on the same port
- launches one harness instance per MCP session
- extracts the harness tool list from the intercepted chat completions request
- maps MCP `tools/call` to harness tool calls and returns the harness tool result back to MCP
- stops the harness process when the MCP session is closed

## Endpoints

Default port: `9330`

- `POST /mcp`
- `POST /harness_to_mcp/mcp`
- `GET /harness_to_mcp/v1/models`
- `POST /harness_to_mcp/v1/chat/completions`

The two MCP paths are equivalent.

## Install

```bash
pip install -e .
```

## Run the server

```bash
harness_to_mcp --port 9330
```

## Launch opencode against the hijack API

```bash
harness_to_mcp opencode --port 9330
```

The helper command writes a temporary `opencode.json`, points `XDG_CONFIG_HOME` to it, and starts `opencode run` with the bootstrap prompt:

```text
<|harness_to_mcp|> MCP initialize -> launch harness
```

If no local `harness_to_mcp` server is already listening on that port, the command starts an embedded server automatically before launching `opencode`.

## Python API

```python
from harness_to_mcp import HarnessToMcp

with HarnessToMcp(port=9330) as server:
    print(server.mcp_url)
    print(server.hijack_base_url)
```

## Notes

- the first implementation targets OpenAI chat completions and `opencode`
- the bridge keeps the intercepted harness request alive with heartbeat chunks every 600 seconds while waiting for the next MCP tool call
- if the harness does not reconnect to the hijack API within 30 seconds, MCP requests fail with a hijack-not-connected error
