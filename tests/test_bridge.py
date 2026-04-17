import asyncio
import contextlib
import logging
from types import SimpleNamespace

import harness_to_mcp.bridge as bridge_module
from harness_to_mcp.adapters import HijackRequest
from harness_to_mcp.bridge import ActiveHijackRequest, HarnessSessionBridge


def test_bridge_call_tool_uses_chat_safe_call_id() -> None:
    async def run() -> None:
        session = HarnessSessionBridge(
            session_id="session-1",
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers={},
            default_launcher_name=None,
        )
        loop = asyncio.get_running_loop()
        active_request = ActiveHijackRequest(
            model="demo-model",
            stream=True,
            response_future=loop.create_future(),
            created_at=0.0,
        )
        session.active_request = active_request
        original_uuid4 = bridge_module.uuid4
        bridge_module.uuid4 = lambda: SimpleNamespace(hex="abc123")
        try:
            task = asyncio.create_task(session.call_tool("read", {"path": "README.md"}))
            payload = await active_request.response_future
            assert payload.tool_call is not None
            assert payload.tool_call.call_id == "callabc123"
            result_future = session.pending_tool_results.pop("callabc123")
            result_future.set_result("ok")
            assert await task == "ok"
        finally:
            bridge_module.uuid4 = original_uuid4
            await session.close()

    asyncio.run(run())


def test_bridge_logs_tool_call_dispatch_and_success(caplog) -> None:
    async def run() -> None:
        session = HarnessSessionBridge(
            session_id="session-1",
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers={},
            default_launcher_name=None,
        )
        loop = asyncio.get_running_loop()
        active_request = ActiveHijackRequest(
            model="demo-model",
            stream=True,
            response_future=loop.create_future(),
            created_at=0.0,
        )
        session.active_request = active_request
        original_uuid4 = bridge_module.uuid4
        bridge_module.uuid4 = lambda: SimpleNamespace(hex="abc123")
        try:
            with caplog.at_level(logging.INFO, logger="harness_to_mcp.bridge"):
                task = asyncio.create_task(session.call_tool("read", {"path": "README.md"}))
                await active_request.response_future
                session.pending_tool_results.pop("callabc123").set_result("ok")
                assert await task == "ok"
        finally:
            bridge_module.uuid4 = original_uuid4
            await session.close()

    asyncio.run(run())
    assert "Dispatching tool call callabc123 (read) for session session-1" in caplog.text
    assert "Tool call callabc123 succeeded for session session-1" in caplog.text


def test_bridge_logs_harness_disconnect(caplog) -> None:
    async def run() -> None:
        session = HarnessSessionBridge(
            session_id="session-1",
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers={},
            default_launcher_name=None,
        )
        loop = asyncio.get_running_loop()
        active_request = ActiveHijackRequest(
            model="demo-model",
            stream=True,
            response_future=loop.create_future(),
            created_at=0.0,
        )
        session.active_request = active_request
        with caplog.at_level(logging.INFO, logger="harness_to_mcp.bridge"):
            await session.release_hijack_request(active_request)
        with contextlib.suppress(RuntimeError):
            await active_request.response_future
        await session.close()

    asyncio.run(run())
    assert "Harness disconnected for session session-1" in caplog.text


def test_bridge_logs_harness_connect(caplog) -> None:
    async def run() -> None:
        session = HarnessSessionBridge(
            session_id="session-1",
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers={},
            default_launcher_name=None,
        )
        active_request = None
        try:
            with caplog.at_level(logging.INFO, logger="harness_to_mcp.bridge"):
                active_request = await session.on_hijack_request(
                    adapter_name="openai_chat",
                    request=HijackRequest(model="demo-model", stream=True, tools=[], tool_results=[]),
                )
        finally:
            await session.close()
            if active_request is not None:
                with contextlib.suppress(RuntimeError):
                    await active_request.response_future

    asyncio.run(run())
    assert "Harness connected for session session-1 via openai_chat" in caplog.text
