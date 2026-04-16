from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .openai_chat import HIJACK_MODEL_ID

HIJACK_PROVIDER_ID = "harness_to_mcp"
LAUNCH_PROMPT = "<|harness_to_mcp|> MCP initialize -> launch harness"


@dataclass(slots=True)
class OpencodeRuntime:
    session_token: str
    base_url: str
    tempdir: tempfile.TemporaryDirectory[str]
    config_dir: Path
    config_path: Path
    log_path: Path
    env: dict[str, str]
    command: list[str]

    def cleanup(self) -> None:
        self.tempdir.cleanup()


def create_runtime(
    *,
    base_url: str,
    session_token: str | None = None,
    prompt: str = LAUNCH_PROMPT,
    json_format: bool = True,
) -> OpencodeRuntime:
    session_token = session_token or uuid.uuid4().hex
    tempdir = tempfile.TemporaryDirectory(prefix="harness_to_mcp_opencode_")
    config_dir = Path(tempdir.name) / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "opencode.json"
    config_path.write_text(build_config(base_url=base_url, session_token=session_token), encoding="utf-8")
    log_path = config_dir / "opencode.log"
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = tempdir.name
    command = build_run_command(prompt=prompt, json_format=json_format)
    return OpencodeRuntime(
        session_token=session_token,
        base_url=base_url,
        tempdir=tempdir,
        config_dir=config_dir,
        config_path=config_path,
        log_path=log_path,
        env=env,
        command=command,
    )


def build_config(*, base_url: str, session_token: str) -> str:
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}",
        "provider": {
            HIJACK_PROVIDER_ID: {
                "name": "HarnessToMcp",
                "options": {
                    "baseURL": base_url.rstrip("/"),
                    "apiKey": session_token,
                },
                "models": {
                    HIJACK_MODEL_ID: {
                        "tool_call": True,
                        "reasoning": False,
                        "temperature": True,
                    }
                },
            }
        },
    }
    return json.dumps(config, indent=2) + "\n"


def build_run_command(*, prompt: str = LAUNCH_PROMPT, json_format: bool = True) -> list[str]:
    command = ["opencode", "run", "--model", f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}"]
    if json_format:
        command.extend(["--format", "json"])
    command.append(prompt)
    return command


def run_opencode(
    *,
    base_url: str,
    session_token: str | None = None,
    prompt: str = LAUNCH_PROMPT,
    workdir: str | None = None,
    json_format: bool = True,
) -> int:
    runtime = create_runtime(
        base_url=base_url,
        session_token=session_token,
        prompt=prompt,
        json_format=json_format,
    )
    try:
        return subprocess.run(runtime.command, cwd=workdir, env=runtime.env).returncode
    finally:
        runtime.cleanup()
