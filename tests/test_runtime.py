import json
import logging
import urllib.request

from harness_to_mcp import HarnessToMcp
from harness_to_mcp.openai_chat import HIJACK_MODEL_ID
from harness_to_mcp.server import _enable_default_logging, _hijack_server_is_ready, _is_local_host, create_app
from starlette.testclient import TestClient


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


def test_explicit_mcp_session_id_is_restored_after_server_restart() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1"},
        },
    }
    with HarnessToMcp(port=0) as first_server:
        request = urllib.request.Request(
            first_server.mcp_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            session_id = response.headers["mcp-session-id"]
    with HarnessToMcp(port=0) as restarted_server:
        request = urllib.request.Request(
            restarted_server.mcp_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "mcp-session-id": session_id,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            assert response.headers["mcp-session-id"] == session_id
    assert body["result"]["serverInfo"]["name"] == "harness_to_mcp"


def test_restored_mcp_session_accepts_non_initialize_request() -> None:
    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1"},
        },
    }
    with TestClient(create_app(port=19411)) as first_client:
        response = first_client.post("/mcp", json=initialize_payload)
        session_id = response.headers["mcp-session-id"]

    app = create_app(port=19412)

    async def fake_ensure_tools_ready(*args, **kwargs):
        return []

    app.state.harness_to_mcp.registry.ensure_tools_ready = fake_ensure_tools_ready
    with TestClient(app) as restarted_client:
        response = restarted_client.post(
            "/mcp",
            headers={"mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["tools"] == []


def test_default_logging_is_enabled_for_package_logger() -> None:
    package_logger = logging.getLogger("harness_to_mcp")
    root_logger = logging.getLogger()
    original_root_handlers = root_logger.handlers[:]
    original_root_level = root_logger.level
    original_handlers = package_logger.handlers[:]
    original_level = package_logger.level
    original_propagate = package_logger.propagate
    try:
        root_logger.handlers.clear()
        package_logger.setLevel(logging.NOTSET)
        package_logger.propagate = True
        _enable_default_logging()
        assert package_logger.level == logging.INFO
        assert root_logger.handlers
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers.clear()
        root_logger.handlers.extend(original_root_handlers)
        root_logger.setLevel(original_root_level)
        package_logger.handlers.clear()
        package_logger.handlers.extend(original_handlers)
        package_logger.setLevel(original_level)
        package_logger.propagate = original_propagate
