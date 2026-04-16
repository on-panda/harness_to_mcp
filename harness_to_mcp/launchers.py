from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .adapters import HIJACK_MODEL_ID

HIJACK_PROVIDER_ID = "harness_to_mcp"
LAUNCH_PROMPT = "<|harness_to_mcp|> MCP initialize -> launch harness"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-20250514"
CODEX_SESSION_TOKEN_ENV = "HARNESS_TO_MCP_CODEX_KEY"


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
        config_dir = Path(tempdir.name) / "opencode"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "opencode.json"
        config_path.write_text(_opencode_config(f"{base_url_root.rstrip('/')}/v1", session_token), encoding="utf-8")
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = tempdir.name
        return HarnessRuntime(
            session_token=session_token,
            tempdir=tempdir,
            env=env,
            command=["opencode", "run", "--model", f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}", "--format", "json", prompt],
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


def build_launchers() -> dict[str, HarnessLauncher]:
    launchers = [OpencodeLauncher(), CodexLauncher(), ClaudeLauncher()]
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
