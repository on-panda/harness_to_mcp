from .adapters import HIJACK_MODEL_ID
from .launchers import HIJACK_PROVIDER_ID, LAUNCH_PROMPT, OpencodeLauncher, _opencode_config


def create_runtime(*, base_url: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT, json_format: bool = True):
    if not json_format:
        raise ValueError("opencode runtime always uses JSON format")
    launcher = OpencodeLauncher()
    return launcher.create_runtime(base_url_root=base_url.rsplit('/v1', 1)[0], session_token=session_token, prompt=prompt)


def build_config(*, base_url: str, session_token: str) -> str:
    return _opencode_config(base_url.rstrip("/"), session_token)


def build_run_command(*, prompt: str = LAUNCH_PROMPT, json_format: bool = True) -> list[str]:
    if not json_format:
        raise ValueError("opencode runtime always uses JSON format")
    return ["opencode", "run", "--model", f"{HIJACK_PROVIDER_ID}/{HIJACK_MODEL_ID}", "--format", "json", prompt]


def run_opencode(*, base_url: str, session_token: str | None = None, prompt: str = LAUNCH_PROMPT, workdir: str | None = None, json_format: bool = True) -> int:
    if not json_format:
        raise ValueError("opencode runtime always uses JSON format")
    launcher = OpencodeLauncher()
    return launcher.run(base_url_root=base_url.rsplit('/v1', 1)[0], session_token=session_token, prompt=prompt, workdir=workdir)
