# 开发文档

## 流程

1. MCP `initialize` 创建一个 streamable HTTP session。
2. 在纯 server 模式下，server 只等待 harness 请求通过某个 hijack API 接入。
3. 在 helper 模式下，子命令会启动一个同机 server 和一个 harness 进程，并把该 harness 绑定到第一个 MCP session id。
4. harness 向某个 hijack API transport 发送请求：
   - OpenAI chat completions
   - OpenAI responses（`POST` 或 `WebSocket`）
   - Anthropic messages
5. hijack API 从该请求中提取 tool list，并通过 MCP `tools/list` 暴露出去。
6. MCP `tools/call` 会把等待中的 hijack 请求解析为匹配 wire API 的 tool call payload。
7. harness 执行 tool，追加 tool result，并发送新的 LLM API 请求。
8. hijack API 匹配 tool call id，完成挂起的 MCP call，并继续保持新的 harness 请求打开，等待下一次 MCP tool call。

## Initialize metadata

- 第一个带 tools 的 hijack 请求会被缓存为 session bootstrap request
- `initialize.result.instructions` 会在经过 harness-specific normalization 后，镜像捕获到的 harness bootstrap prompts
- `initialize.result.capabilities.experimental.harness_to_mcp.initial_request` 存储第一个带 tools 的 hijack 请求的原始 JSON body
- `initialize.result.capabilities.experimental.harness_to_mcp.harness_info` 存储 harness metadata；当 harness 名称已知时，`harness_info.harness` 会被设置为该名称
- 在 helper 模式下，`initialize` 会等待这个 bootstrap request；在纯 server 模式下，如果外部 harness 尚未接入，该字段可以保持为空
- 在纯 server 模式下，如果接入的 wire protocol 无法明确识别出唯一 harness，`harness_info` 可以保持为空

## Instructions templates

- `codex`（`openai_responses`）：原样保留顶层 `instructions`，把 developer messages 和非最终 user messages 移入 `<codex_harness_context>`，并把最终 user message 视为 `<codex_initial_user_prompt>`
- `claude`（`anthropic_messages`）：把 `system[*].text` 合并为 base instructions，把较早的 user text blocks（例如 reminder/context blocks）保留在 `<claude_harness_context>` 中，并把最后一个 user text block 作为 `<claude_initial_user_prompt>`
- `opencode` / `openclaw`（`openai_chat`）：把 system messages 合并为 base instructions，把 developer messages 和非最终 user messages 保留在 `<opencode_harness_context>` / `<openclaw_harness_context>` 中，并把最终 user message 作为 initial user prompt
- 会从派生出的 initial user prompt 中剥离 `<|harness_to_mcp_start|> ... <|harness_to_mcp_end|>` 这类 bootstrap markers，避免 helper launch prompts 泄漏到面向 MCP 的 instructions text 中

## Config safety

- helper launch commands 不会修改当前本地 harness configs
- 对 `opencode`、`openclaw`、`codex` 和 `claude` 使用 `tempfile.gettempdir()/harness_to_mcp/{harness}` 下固定的临时 runtime roots
- `openclaw` helper 模式下，每个 `harness_to_mcp openclaw` 进程使用一个共享的隔离 gateway sidecar，而不是每个 MCP session 启动一个 gateway

## Debug tips

- `harness_to_mcp --port 9330`
- `harness_to_mcp openclaw --port 9330`
- `harness_to_mcp codex --port 9330`
- `harness_to_mcp claude --port 9330`
- `curl http://127.0.0.1:9330/harness_to_mcp/health`
