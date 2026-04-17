import json
from pathlib import Path
from unittest.mock import patch

from harness_to_mcp.launchers import (
    CODEX_SESSION_TOKEN_ENV,
    LAUNCH_PROMPT,
    OPENCLAW_GATEWAY_PORT_ENV,
    OPENCLAW_SESSION_TOKEN_ENV,
    OPENCLAW_WORKDIR_ENV,
    ClaudeLauncher,
    CodexLauncher,
    OpencodeLauncher,
    OpenclawLauncher,
)


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


def test_openclaw_launcher_uses_temp_config_and_chat_provider() -> None:
    runtime = OpenclawLauncher().create_runtime(base_url_root="http://127.0.0.1:9330/harness_to_mcp", session_token="token-1")
    try:
        assert runtime.env[OPENCLAW_GATEWAY_PORT_ENV]
        assert runtime.env[OPENCLAW_SESSION_TOKEN_ENV] == "token-1"
        assert runtime.env["OPENCLAW_HOME"]
        assert runtime.env["OPENCLAW_STATE_DIR"]
        assert runtime.env["OPENCLAW_CONFIG_PATH"]
        assert runtime.env["HOME"]
        config = json.loads(Path(runtime.env["OPENCLAW_CONFIG_PATH"]).read_text(encoding="utf-8"))
        assert config["browser"]["enabled"] is True
        assert config["browser"]["extraArgs"] == ["--use-mock-keychain"]
        assert config["agents"]["defaults"]["workspace"] == f"${{{OPENCLAW_WORKDIR_ENV}}}"
        assert config["agents"]["defaults"]["skipBootstrap"] is True
        assert config["gateway"]["mode"] == "local"
        assert config["gateway"]["bind"] == "loopback"
        assert config["gateway"]["auth"]["mode"] == "none"
        assert config["gateway"]["port"] == int(runtime.env[OPENCLAW_GATEWAY_PORT_ENV])
        assert config["models"]["providers"]["harness_to_mcp"]["api"] == "openai-completions"
        assert runtime.command[:4] == ["openclaw", "agent", "--local", "--session-id"]
        assert runtime.command[4] == "token-1"
        assert runtime.command[-3:] == ["--message", LAUNCH_PROMPT, "--json"]
    finally:
        runtime.cleanup()


def test_openclaw_launcher_reuses_shared_gateway_for_helper_processes() -> None:
    class _DummyProcess:
        def __init__(self, command: list[str]) -> None:
            self.command = command
            self.pid = 12345
            self._returncode = None

        def poll(self):
            return self._returncode

        def terminate(self) -> None:
            self._returncode = 0

        def wait(self, timeout=None) -> int:
            self._returncode = 0
            return 0

        def kill(self) -> None:
            self._returncode = -9

    class _ReadyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    launcher = OpenclawLauncher()
    processes: list[_DummyProcess] = []

    def fake_popen(command, **kwargs):
        process = _DummyProcess(command)
        processes.append(process)
        return process

    with (
        patch("harness_to_mcp.launchers.subprocess.Popen", side_effect=fake_popen),
        patch("harness_to_mcp.launchers.urllib.request.urlopen", return_value=_ReadyResponse()),
    ):
        runtime_1, process_1 = launcher.create_process(
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            session_token="token-1",
            prompt=LAUNCH_PROMPT,
            workdir="/tmp/demo",
        )
        runtime_2, process_2 = launcher.create_process(
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            session_token="token-1",
            prompt=LAUNCH_PROMPT,
            workdir="/tmp/demo",
        )
        gateway_processes = [process for process in processes if process.command[:2] == ["openclaw", "gateway"]]
        agent_processes = [process for process in processes if process.command[:2] == ["openclaw", "agent"]]
        assert len(gateway_processes) == 1
        assert len(agent_processes) == 2
        assert process_1 is agent_processes[0]
        assert process_2 is agent_processes[1]
        assert runtime_1.tempdir is None
        assert runtime_2.tempdir is None
        launcher.shutdown()
