import json
import urllib.request

from harness_to_mcp import HarnessToMcp
from harness_to_mcp.openai_chat import HIJACK_MODEL_ID
from harness_to_mcp.server import _hijack_server_is_ready, _is_local_host


def test_local_host_detection() -> None:
    assert _is_local_host("127.0.0.1") is True
    assert _is_local_host("0.0.0.0") is True
    assert _is_local_host("localhost") is True
    assert _is_local_host("example.com") is False


def test_context_manager_serves_models_endpoint() -> None:
    with HarnessToMcp(port=0) as server:
        with urllib.request.urlopen(f"{server.hijack_base_url}/models", timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["data"][0]["id"] == HIJACK_MODEL_ID
        assert _hijack_server_is_ready(server.hijack_base_url) is True
