import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace

import anyio
import harness_to_mcp.bridge as bridge_module
from mcp import types
from harness_to_mcp.adapters import HijackRequest, InitialPrompts, ToolResult
from harness_to_mcp.bridge import ActiveHijackRequest, HarnessSessionBridge, HarnessSessionRegistry
from harness_to_mcp.launchers import build_launchers


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
            assert payload.tool_calls is not None
            assert payload.tool_calls[0].call_id == "callabc123"
            result_future = session.pending_tool_results.pop("callabc123")
            result_future.set_result("ok")
            assert await task == "ok"
        finally:
            bridge_module.uuid4 = original_uuid4
            await session.close()

    asyncio.run(run())


def test_bridge_batches_tool_calls_from_same_session() -> None:
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
        bridge_module.uuid4 = iter(
            [
                SimpleNamespace(hex="0001"),
                SimpleNamespace(hex="0002"),
                SimpleNamespace(hex="0003"),
                SimpleNamespace(hex="0004"),
                SimpleNamespace(hex="0005"),
            ]
        ).__next__
        try:
            tasks = [
                asyncio.create_task(session.call_tool("bash", {"command": "pwd", "description": "显示当前工作目录"})),
                asyncio.create_task(session.call_tool("bash", {"command": "git status", "description": "查看 git 仓库状态"})),
                asyncio.create_task(session.call_tool("bash", {"command": "git branch -a", "description": "查看所有分支"})),
                asyncio.create_task(session.call_tool("bash", {"command": "git log --oneline -10", "description": "查看最近10条提交记录"})),
                asyncio.create_task(session.call_tool("bash", {"command": "ls -la", "description": "列出目录内容"})),
            ]
            payload = await active_request.response_future
            assert [tool_call.name for tool_call in payload.tool_calls or []] == ["bash"] * 5
            assert [tool_call.arguments for tool_call in payload.tool_calls or []] == [
                {"command": "pwd", "description": "显示当前工作目录"},
                {"command": "git status", "description": "查看 git 仓库状态"},
                {"command": "git branch -a", "description": "查看所有分支"},
                {"command": "git log --oneline -10", "description": "查看最近10条提交记录"},
                {"command": "ls -la", "description": "列出目录内容"},
            ]
            next_request = await session.on_hijack_request(
                adapter_name="openai_chat",
                request=HijackRequest(
                    model="demo-model",
                    stream=True,
                    tools=[],
                    tool_results=[
                        ToolResult(tool_call_id="call0001", content="/tmp/demo"),
                        ToolResult(tool_call_id="call0002", content="clean"),
                        ToolResult(tool_call_id="call0003", content="* main"),
                        ToolResult(tool_call_id="call0004", content="abc123"),
                        ToolResult(tool_call_id="call0005", content="total 8"),
                    ],
                ),
            )
            assert await asyncio.gather(*tasks) == ["/tmp/demo", "clean", "* main", "abc123", "total 8"]
            await session.release_hijack_request(next_request)
            with contextlib.suppress(RuntimeError):
                await next_request.response_future
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
    assert "Dispatching tool call read" in caplog.text
    assert "Tool call read succeeded" in caplog.text


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
    assert "Harness disconnected" in caplog.text


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
    assert "Harness connected via openai_chat" in caplog.text


def test_bridge_renders_initialize_instructions_from_initial_prompts() -> None:
    session = HarnessSessionBridge(
        session_id="session-1",
        workdir="/tmp/demo",
        base_url_root="http://127.0.0.1:9330/harness_to_mcp",
        launchers={},
        default_launcher_name="codex",
    )
    session.initial_prompts = InitialPrompts(
        instructions="Base instructions",
        harness_context="Developer context",
        user_prompt="Hello from probe",
    )
    try:
        assert session._render_initialize_instructions() == (
            "Base instructions\n\n"
            "<codex_harness_context>\nDeveloper context\n</codex_harness_context>\n\n"
            "<codex_initial_user_prompt>\nHello from probe\n</codex_initial_user_prompt>"
        )
    finally:
        asyncio.run(session.close())


def test_plain_mode_mcp_session_adopts_existing_hijack_session() -> None:
    async def run() -> None:
        registry = HarnessSessionRegistry(
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers=build_launchers(),
            default_launcher_name=None,
        )
        active_request = await registry.on_hijack_request(
            "external-1",
            adapter_name="openai_responses",
            request=HijackRequest(
                model="demo-model",
                stream=True,
                tools=[types.Tool(name="ping", description="Run ping", inputSchema={"type": "object"})],
                tool_results=[],
                initial_prompts=InitialPrompts(
                    instructions="Base instructions",
                    harness_context="Developer context",
                    user_prompt="Run ping",
                ),
                initial_request={"model": "demo-model", "tools": [{"name": "ping"}]},
            ),
        )
        try:
            instructions = await registry.get_initialize_instructions("mcp-1", wait_for_tools=True, timeout_seconds=0.1)
            initial_request = await registry.get_initialize_initial_request("mcp-1", wait_for_tools=True, timeout_seconds=0.1)
            tools = await registry.ensure_tools_ready("mcp-1", timeout_seconds=0.1)
            assert instructions == (
                "Base instructions\n\n"
                "<codex_harness_context>\nDeveloper context\n</codex_harness_context>\n\n"
                "<codex_initial_user_prompt>\nRun ping\n</codex_initial_user_prompt>"
            )
            assert initial_request == {"model": "demo-model", "tools": [{"name": "ping"}]}
            assert [tool.name for tool in tools] == ["ping"]
        finally:
            await registry.close_session("mcp-1")
            await registry.close()
            with contextlib.suppress(RuntimeError):
                await active_request.response_future

    asyncio.run(run())


def test_plain_mode_hijack_session_adopts_existing_mcp_session() -> None:
    async def run() -> None:
        registry = HarnessSessionRegistry(
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers=build_launchers(),
            default_launcher_name=None,
        )
        wait_task = asyncio.create_task(registry.ensure_tools_ready("mcp-1", timeout_seconds=1))
        await anyio.sleep(0)
        active_request = await registry.on_hijack_request(
            "external-1",
            adapter_name="openai_responses",
            request=HijackRequest(
                model="demo-model",
                stream=True,
                tools=[types.Tool(name="ping", description="Run ping", inputSchema={"type": "object"})],
                tool_results=[],
                initial_prompts=InitialPrompts(instructions="Base instructions"),
                initial_request={"model": "demo-model", "tools": [{"name": "ping"}]},
            ),
        )
        try:
            tools = await wait_task
            assert [tool.name for tool in tools] == ["ping"]
        finally:
            await registry.close_session("mcp-1")
            await registry.close()
            with contextlib.suppress(RuntimeError):
                await active_request.response_future

    asyncio.run(run())


def test_plain_mode_close_session_only_unbinds_mcp_session() -> None:
    async def run() -> None:
        registry = HarnessSessionRegistry(
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers=build_launchers(),
            default_launcher_name=None,
        )
        active_request = await registry.on_hijack_request(
            "external-1",
            adapter_name="openai_responses",
            request=HijackRequest(
                model="demo-model",
                stream=True,
                tools=[types.Tool(name="ping", description="Run ping", inputSchema={"type": "object"})],
                tool_results=[],
                initial_prompts=InitialPrompts(instructions="Base instructions"),
                initial_request={"model": "demo-model", "tools": [{"name": "ping"}]},
            ),
        )
        try:
            assert [tool.name for tool in await registry.ensure_tools_ready("mcp-1", timeout_seconds=0.1)] == ["ping"]
            await registry.close_session("mcp-1")
            assert [tool.name for tool in await registry.ensure_tools_ready("mcp-2", timeout_seconds=0.1)] == ["ping"]
        finally:
            await registry.close()
            with contextlib.suppress(RuntimeError):
                await active_request.response_future

    asyncio.run(run())


def test_plain_mode_does_not_restart_inferred_helper_launcher() -> None:
    async def run() -> None:
        session = HarnessSessionBridge(
            session_id="external-1",
            workdir="/tmp/demo",
            base_url_root="http://127.0.0.1:9330/harness_to_mcp",
            launchers=build_launchers(),
            default_launcher_name=None,
        )
        active_request = await session.on_hijack_request(
            adapter_name="openai_responses",
            request=HijackRequest(model="demo-model", stream=True, tools=[], tool_results=[]),
        )
        try:
            assert session.launcher_name == "codex"
            assert session._should_restart_harness(time.monotonic() + 10) is False
        finally:
            await session.close()
            with contextlib.suppress(RuntimeError):
                await active_request.response_future

    asyncio.run(run())
