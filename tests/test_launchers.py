from harness_to_mcp.launchers import CODEX_SESSION_TOKEN_ENV, ClaudeLauncher, CodexLauncher, OpencodeLauncher


def test_opencode_launcher_uses_temp_xdg_dirs() -> None:
    runtime = OpencodeLauncher().create_runtime(base_url_root="http://127.0.0.1:9330/harness_to_mcp", session_token="token-1")
    try:
        assert runtime.env["XDG_CONFIG_HOME"]
        assert runtime.env["XDG_DATA_HOME"]
        assert runtime.env["XDG_CACHE_HOME"]
        assert runtime.env["XDG_STATE_HOME"]
        assert runtime.command[:4] == ["opencode", "run", "--model", "harness_to_mcp/harness_to_mcp_hijack_api"]
    finally:
        runtime.cleanup()


def test_codex_launcher_uses_temp_home_and_responses_provider() -> None:
    runtime = CodexLauncher().create_runtime(base_url_root="http://127.0.0.1:9330/harness_to_mcp", session_token="token-1")
    try:
        assert runtime.env[CODEX_SESSION_TOKEN_ENV] == "token-1"
        assert runtime.env["HOME"]
        joined = " ".join(runtime.command)
        assert "wire_api=\"responses\"" in joined
        assert "http://127.0.0.1:9330/harness_to_mcp/v1" in joined
    finally:
        runtime.cleanup()


def test_claude_launcher_uses_temp_config_dir() -> None:
    runtime = ClaudeLauncher().create_runtime(base_url_root="http://127.0.0.1:9330/harness_to_mcp", session_token="token-1")
    try:
        assert runtime.env["CLAUDE_CONFIG_DIR"]
        assert runtime.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9330/harness_to_mcp"
        assert runtime.env["ANTHROPIC_API_KEY"] == "token-1"
        assert runtime.command[0] == "claude"
    finally:
        runtime.cleanup()
