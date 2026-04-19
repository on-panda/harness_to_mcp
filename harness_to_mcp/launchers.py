from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .adapters import HIJACK_MODEL_ID

HIJACK_PROVIDER_ID = "harness_to_mcp"
LAUNCH_PROMPT = "<|harness_to_mcp_start|> MCP initialize -> launch harness<|harness_to_mcp_end|>"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-20250514"
CODEX_SESSION_TOKEN_ENV = "HARNESS_TO_MCP_CODEX_KEY"
OPENCLAW_SESSION_TOKEN_ENV = "HARNESS_TO_MCP_OPENCLAW_KEY"
OPENCLAW_WORKDIR_ENV = "HARNESS_TO_MCP_OPENCLAW_WORKDIR"
OPENCLAW_GATEWAY_PORT_ENV = "OPENCLAW_GATEWAY_PORT"


@dataclass(slots=True)
class HarnessRuntime:
    session_token: str
    tempdir: tempfile.TemporaryDirectory[str] | None
    env: dict[str, str]
    command: list[str]
    log_path: Path | None = None

    def cleanup(self) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


class HarnessLauncher:
    name = ""
    adapter_name = ""

    def create_runtime(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT) -> HarnessRuntime:
        raise NotImplementedError

    def shutdown(self) -> None:
        pass

    def create_process(self, *, base_url_root: str, session_token: str, prompt: str, workdir: str) -> tuple[HarnessRuntime, subprocess.Popen[str]]:
        runtime = self.create_runtime(base_url_root=base_url_root, session_token=session_token, prompt=prompt)
        stdout = subprocess.DEVNULL
        if runtime.log_path is not None:
            stdout = runtime.log_path.open("a", encoding="utf-8", buffering=1)
        process = subprocess.Popen(
            runtime.command,
            cwd=workdir,
            env=runtime.env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return runtime, process

    def run(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT, workdir: str | None = None) -> int:
        runtime = self.create_runtime(base_url_root=base_url_root, session_token=session_token, prompt=prompt)
        try:
            return subprocess.run(runtime.command, cwd=workdir, env=runtime.env).returncode
        finally:
            runtime.cleanup()


class OpencodeLauncher(HarnessLauncher):
    name = "opencode"
    adapter_name = "openai_chat"

    def create_runtime(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT) -> HarnessRuntime:
        session_token = session_token or uuid.uuid4().hex
        tempdir = tempfile.TemporaryDirectory(prefix="harness_to_mcp_opencode_")
        temp_root = Path(tempdir.name)
        config_dir = temp_root / "config" / "opencode"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(_opencode_config(f"{base_url_root.rstrip('/')}/v1", session_token), encoding="utf-8")
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(temp_root / "config")
        env["XDG_DATA_HOME"] = str(temp_root / "data")
        env["XDG_CACHE_HOME"] = str(temp_root / "cache")
        env["XDG_STATE_HOME"] = str(temp_root / "state")
        return HarnessRuntime(
            session_token=session_token,
            tempdir=tempdir,
            env=env,
            command=[
                "opencode",
                "run",
                "--dangerously-skip-permissions",
                "--model",
                f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}",
                "--format",
                "json",
                prompt,
            ],
            log_path=config_dir / "opencode.log",
        )


class CodexLauncher(HarnessLauncher):
    name = "codex"
    adapter_name = "openai_responses"

    def create_runtime(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT) -> HarnessRuntime:
        session_token = session_token or uuid.uuid4().hex
        tempdir = tempfile.TemporaryDirectory(prefix="harness_to_mcp_codex_")
        home = Path(tempdir.name)
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = tempdir.name
        env[CODEX_SESSION_TOKEN_ENV] = session_token
        base_url = f"{base_url_root.rstrip('/')}/v1"
        command = [
            "codex",
            "exec",
            "--json",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'preferred_auth_method="apikey"',
            "-c",
            f'model="{HIJACK_MODEL_ID}"',
            "-c",
            'model_provider="harness_to_mcp"',
            "-c",
            f'model_providers.harness_to_mcp={{name="HarnessToMcp",base_url="{base_url}",env_key="{CODEX_SESSION_TOKEN_ENV}",wire_api="responses"}}',
            prompt,
        ]
        return HarnessRuntime(session_token=session_token, tempdir=tempdir, env=env, command=command)


class ClaudeLauncher(HarnessLauncher):
    name = "claude"
    adapter_name = "anthropic_messages"

    def create_runtime(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT) -> HarnessRuntime:
        session_token = session_token or uuid.uuid4().hex
        tempdir = tempfile.TemporaryDirectory(prefix="harness_to_mcp_claude_")
        config_dir = Path(tempdir.name) / ".claude"
        config_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        env["ANTHROPIC_BASE_URL"] = base_url_root.rstrip("/")
        env["ANTHROPIC_API_KEY"] = session_token
        command = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            DEFAULT_CLAUDE_MODEL,
            prompt,
        ]
        return HarnessRuntime(session_token=session_token, tempdir=tempdir, env=env, command=command)


class OpenclawLauncher(HarnessLauncher):
    name = "openclaw"
    adapter_name = "openai_chat"

    def __init__(self) -> None:
        self.helper_runtime: HarnessRuntime | None = None
        self.gateway_process: subprocess.Popen[str] | None = None
        self.gateway_port: int | None = None

    def create_runtime(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT) -> HarnessRuntime:
        session_token = session_token or uuid.uuid4().hex
        gateway_port = _pick_unused_port()
        tempdir = tempfile.TemporaryDirectory(prefix="harness_to_mcp_openclaw_")
        temp_root = Path(tempdir.name)
        home = temp_root / "home"
        home.mkdir(parents=True, exist_ok=True)
        config_path = temp_root / "openclaw.json"
        config_path.write_text(_openclaw_config(f"{base_url_root.rstrip('/')}/v1", gateway_port), encoding="utf-8")
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["OPENCLAW_HOME"] = str(home)
        env["OPENCLAW_STATE_DIR"] = str(temp_root / "state")
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env[OPENCLAW_GATEWAY_PORT_ENV] = str(gateway_port)
        env[OPENCLAW_SESSION_TOKEN_ENV] = session_token
        return HarnessRuntime(
            session_token=session_token,
            tempdir=tempdir,
            env=env,
            command=[
                "openclaw",
                "agent",
                "--local",
                "--session-id",
                session_token,
                "--message",
                prompt,
                "--json",
            ],
            log_path=temp_root / "openclaw.log",
        )

    def create_process(self, *, base_url_root: str, session_token: str, prompt: str, workdir: str) -> tuple[HarnessRuntime, subprocess.Popen[str]]:
        helper_runtime = self.helper_runtime
        if helper_runtime is None:
            helper_runtime = self.create_runtime(base_url_root=base_url_root, session_token=session_token, prompt=prompt)
            self.helper_runtime = helper_runtime
            self.gateway_port = int(helper_runtime.env[OPENCLAW_GATEWAY_PORT_ENV])
        resolved_workdir = str(Path(workdir).resolve())
        helper_runtime.env[OPENCLAW_WORKDIR_ENV] = resolved_workdir
        helper_runtime.env[OPENCLAW_SESSION_TOKEN_ENV] = session_token
        self._ensure_gateway_running(workdir)
        runtime = HarnessRuntime(
            session_token=session_token,
            tempdir=None,
            env=helper_runtime.env.copy(),
            command=helper_runtime.command,
            log_path=helper_runtime.log_path,
        )
        stdout = subprocess.DEVNULL
        if runtime.log_path is not None:
            stdout = runtime.log_path.open("a", encoding="utf-8", buffering=1)
        process = subprocess.Popen(
            runtime.command,
            cwd=workdir,
            env=runtime.env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return runtime, process

    def run(self, *, base_url_root: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT, workdir: str | None = None) -> int:
        runtime = self.create_runtime(base_url_root=base_url_root, session_token=session_token, prompt=prompt)
        runtime.env[OPENCLAW_WORKDIR_ENV] = str(Path(workdir or os.getcwd()).resolve())
        gateway_process = self._start_gateway(runtime=runtime, workdir=workdir or os.getcwd())
        try:
            return subprocess.run(runtime.command, cwd=workdir, env=runtime.env).returncode
        finally:
            _terminate_process(gateway_process)
            runtime.cleanup()

    def shutdown(self) -> None:
        _terminate_process(self.gateway_process)
        self.gateway_process = None
        self.gateway_port = None
        if self.helper_runtime is not None:
            self.helper_runtime.cleanup()
            self.helper_runtime = None

    def _ensure_gateway_running(self, workdir: str) -> None:
        if self.helper_runtime is None:
            return
        if self.gateway_process is not None and self.gateway_process.poll() is None:
            return
        self.gateway_process = self._start_gateway(runtime=self.helper_runtime, workdir=workdir)

    def _start_gateway(self, *, runtime: HarnessRuntime, workdir: str) -> subprocess.Popen[str]:
        gateway_log_path = runtime.log_path.with_name("openclaw-gateway.log") if runtime.log_path is not None else None
        stdout = subprocess.DEVNULL
        if gateway_log_path is not None:
            stdout = gateway_log_path.open("a", encoding="utf-8", buffering=1)
        process = subprocess.Popen(
            [
                "openclaw",
                "gateway",
                "--allow-unconfigured",
                "--bind",
                "loopback",
                "--auth",
                "none",
                "--port",
                runtime.env[OPENCLAW_GATEWAY_PORT_ENV],
                "run",
            ],
            cwd=workdir,
            env=runtime.env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 15
        gateway_url = f"http://127.0.0.1:{runtime.env[OPENCLAW_GATEWAY_PORT_ENV]}/readyz"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("OpenClaw gateway exited before it became ready.")
            try:
                with urllib.request.urlopen(gateway_url, timeout=1):
                    return process
            except (OSError, urllib.error.URLError, urllib.error.HTTPError):
                time.sleep(0.1)
        raise RuntimeError("Timed out while starting OpenClaw gateway.")


def build_launchers() -> dict[str, HarnessLauncher]:
    launchers = [OpencodeLauncher(), CodexLauncher(), ClaudeLauncher(), OpenclawLauncher()]
    return {launcher.name: launcher for launcher in launchers}


def launcher_for_adapter(launchers: dict[str, HarnessLauncher], adapter_name: str) -> str | None:
    matches = [launcher.name for launcher in launchers.values() if launcher.adapter_name == adapter_name]
    if len(matches) == 1:
        return matches[0]
    return None


def _opencode_config(base_url: str, session_token: str) -> str:
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}",
            "provider": {
                HIJACK_PROVIDER_ID: {
                    "name": "HarnessToMcp",
                    "options": {"baseURL": base_url, "apiKey": session_token},
                    "models": {HIJACK_MODEL_ID: {"tool_call": True, "reasoning": False, "temperature": True}},
                }
            },
        },
        indent=2,
    ) + "\n"


def _openclaw_config(base_url: str, gateway_port: int) -> str:
    return json.dumps(
        {
            "browser": {
                "enabled": True,
                "extraArgs": ["--use-mock-keychain"],
            },
            "agents": {
                "defaults": {
                    "workspace": f"${{{OPENCLAW_WORKDIR_ENV}}}",
                    "skipBootstrap": True,
                    "model": {"primary": f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}"},
                }
            },
            "gateway": {
                "mode": "local",
                "port": gateway_port,
                "bind": "loopback",
                "auth": {"mode": "none"},
            },
            "models": {
                "mode": "merge",
                "providers": {
                    HIJACK_PROVIDER_ID: {
                        "baseUrl": base_url,
                        "apiKey": f"${{{OPENCLAW_SESSION_TOKEN_ENV}}}",
                        "api": "openai-completions",
                        "authHeader": True,
                        "models": [
                            {
                                "id": HIJACK_MODEL_ID,
                                "name": "HarnessToMcp",
                                "api": "openai-completions",
                            }
                        ],
                    }
                },
            },
        },
        indent=2,
    ) + "\n"


def _pick_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
