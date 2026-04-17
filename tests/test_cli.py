from harness_to_mcp.opencode import HIJACK_PROVIDER_ID, LAUNCH_PROMPT, build_config, build_run_command
from harness_to_mcp.server import DEFAULT_HOST, DEFAULT_PORT, build_argument_parser


def test_server_parser_defaults() -> None:
    args = build_argument_parser().parse_args([])
    assert args.host == DEFAULT_HOST
    assert args.port == DEFAULT_PORT
    assert args.subcommand is None


def test_opencode_parser_defaults() -> None:
    args = build_argument_parser().parse_args(["opencode"])
    assert args.host == DEFAULT_HOST
    assert args.port == DEFAULT_PORT
    assert args.prompt == LAUNCH_PROMPT


def test_codex_parser_defaults() -> None:
    args = build_argument_parser().parse_args(["codex"])
    assert args.host == DEFAULT_HOST
    assert args.port == DEFAULT_PORT
    assert args.prompt == LAUNCH_PROMPT


def test_claude_parser_defaults() -> None:
    args = build_argument_parser().parse_args(["claude"])
    assert args.host == DEFAULT_HOST
    assert args.port == DEFAULT_PORT
    assert args.prompt == LAUNCH_PROMPT


def test_openclaw_parser_defaults() -> None:
    args = build_argument_parser().parse_args(["openclaw"])
    assert args.host == DEFAULT_HOST
    assert args.port == DEFAULT_PORT
    assert args.prompt == LAUNCH_PROMPT


def test_build_config_embeds_provider_and_token() -> None:
    text = build_config(base_url="http://127.0.0.1:9330/harness_to_mcp/v1", session_token="token-1")
    assert HIJACK_PROVIDER_ID in text
    assert "token-1" in text
    assert "harness_to_mcp_hijack_api" in text


def test_build_run_command_uses_bootstrap_prompt() -> None:
    command = build_run_command()
    assert command[:5] == ["opencode", "run", "--dangerously-skip-permissions", "--model", "harness_to_mcp/harness_to_mcp_hijack_api"]
    assert command[-1] == LAUNCH_PROMPT
