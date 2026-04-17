import asyncio
from types import SimpleNamespace

import harness_to_mcp.bridge as bridge_module
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
