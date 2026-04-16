from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio

from .openai_chat import CompletionPayload, ToolCallSpec, extract_tool_results, extract_tools
from .opencode import OpencodeRuntime, create_runtime

logger = logging.getLogger(__name__)

HIJACK_CONNECT_TIMEOUT_SECONDS = 30
ACTIVE_REQUEST_GRACE_SECONDS = 2


@dataclass(slots=True)
class ActiveHijackRequest:
    model: str
    stream: bool
    response_future: asyncio.Future[CompletionPayload]
    created_at: float


class OpencodeHarnessLauncher:
    def __init__(self, *, base_url: str, prompt: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.prompt = prompt

    def create_process(self, session_id: str, workdir: str) -> tuple[OpencodeRuntime, subprocess.Popen[str]]:
        runtime = create_runtime(base_url=self.base_url, session_token=session_id, prompt=self.prompt)
        with runtime.log_path.open("a", encoding="utf-8", buffering=1) as log_handle:
            process = subprocess.Popen(
                runtime.command,
                cwd=workdir,
                env=runtime.env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return runtime, process


class HarnessSessionBridge:
    def __init__(self, *, session_id: str, workdir: str, launcher: OpencodeHarnessLauncher) -> None:
        self.session_id = session_id
        self.workdir = workdir
        self.launcher = launcher
        self.lock = anyio.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.runtime: OpencodeRuntime | None = None
        self.launch_started_at = 0.0
        self.last_harness_activity_at = 0.0
        self.tools = []
        self.active_request: ActiveHijackRequest | None = None
        self.pending_tool_results: dict[str, asyncio.Future[str]] = {}
        self.mcp_open = True
        self._tools_ready = anyio.Event()
        self._active_request_ready = anyio.Event()

    async def on_initialize(self) -> None:
        async with self.lock:
            self.mcp_open = True
            await self._start_harness_locked(restart=self.process is None or self.process.poll() is not None)

    async def close(self) -> None:
        async with self.lock:
            self.mcp_open = False
            await self._stop_harness_locked()
            self._fail_pending_locked(RuntimeError("MCP session closed."))
            self._clear_active_request_locked(RuntimeError("MCP session closed."))
            self.tools = []
            self._tools_ready = anyio.Event()

    async def ensure_tools_ready(self, timeout_seconds: float) -> list[Any]:
        started_at = time.monotonic()
        await self._ensure_active_request(timeout_seconds)
        if self.tools:
            return self.tools
        remaining = max(0.1, timeout_seconds - (time.monotonic() - started_at))
        event = self._tools_ready
        with anyio.fail_after(remaining):
            await event.wait()
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        active_request = await self._ensure_active_request(HIJACK_CONNECT_TIMEOUT_SECONDS)
        loop = asyncio.get_running_loop()
        call_id = f"call_{uuid4().hex}"
        result_future = loop.create_future()
        response = CompletionPayload(
            model=active_request.model,
            tool_call=ToolCallSpec(call_id=call_id, name=name, arguments=arguments),
        )
        async with self.lock:
            self.pending_tool_results[call_id] = result_future
            if active_request.response_future.done():
                self.pending_tool_results.pop(call_id, None)
                raise RuntimeError("Hijack API request is no longer active.")
            active_request.response_future.set_result(response)
            self.active_request = None
            self._active_request_ready = anyio.Event()
        return await result_future

    async def on_hijack_request(self, body: dict[str, Any], model: str, stream: bool) -> ActiveHijackRequest:
        tool_results = extract_tool_results(body)
        tools = extract_tools(body)
        loop = asyncio.get_running_loop()
        active_request = ActiveHijackRequest(
            model=model,
            stream=stream,
            response_future=loop.create_future(),
            created_at=time.monotonic(),
        )
        async with self.lock:
            self.last_harness_activity_at = time.monotonic()
            for tool_result in tool_results:
                result_future = self.pending_tool_results.pop(tool_result.tool_call_id, None)
                if result_future is not None and not result_future.done():
                    result_future.set_result(tool_result.content)
            if tools:
                self.tools = tools
                self._tools_ready.set()
            self._clear_active_request_locked(RuntimeError("Hijack API request replaced by a newer request."))
            self.active_request = active_request
            self._active_request_ready.set()
        return active_request

    async def release_hijack_request(self, active_request: ActiveHijackRequest) -> None:
        async with self.lock:
            if self.active_request is active_request:
                self._clear_active_request_locked(RuntimeError("Hijack API request closed."))

    async def _ensure_active_request(self, timeout_seconds: float) -> ActiveHijackRequest:
        active_request = self.active_request
        if active_request is not None:
            return active_request
        wait_started_at = time.monotonic()
        if self._should_restart_harness(wait_started_at):
            async with self.lock:
                if self._should_restart_harness(wait_started_at):
                    await self._start_harness_locked(restart=True)
        event = self._active_request_ready
        with anyio.fail_after(timeout_seconds):
            await event.wait()
        active_request = self.active_request
        if active_request is None:
            raise RuntimeError("Hijack API did not connect to harness within 30 seconds.")
        return active_request

    def _should_restart_harness(self, now: float) -> bool:
        if self.process is None or self.process.poll() is not None:
            return True
        if self.active_request is not None:
            return False
        if now - self.last_harness_activity_at <= ACTIVE_REQUEST_GRACE_SECONDS:
            return False
        return True

    async def _start_harness_locked(self, restart: bool) -> None:
        if not self.mcp_open:
            return
        if restart:
            await self._stop_harness_locked()
            self._fail_pending_locked(RuntimeError("Harness restarted before tool result arrived."))
            self._clear_active_request_locked(RuntimeError("Harness restarted."))
            self.tools = []
            self._tools_ready = anyio.Event()
        if self.process is not None and self.process.poll() is None:
            return
        self.runtime, self.process = self.launcher.create_process(self.session_id, self.workdir)
        self.launch_started_at = time.monotonic()
        logger.info("Started harness for session %s with pid %s", self.session_id, self.process.pid)

    async def _stop_harness_locked(self) -> None:
        process = self.process
        runtime = self.runtime
        self.process = None
        self.runtime = None
        if process is not None and process.poll() is None:
            process.terminate()
            await anyio.to_thread.run_sync(self._wait_or_kill_process, process)
            logger.info("Stopped harness for session %s", self.session_id)
        if runtime is not None:
            runtime.cleanup()

    def _fail_pending_locked(self, exc: Exception) -> None:
        for future in self.pending_tool_results.values():
            if not future.done():
                future.set_exception(exc)
        self.pending_tool_results.clear()

    def _clear_active_request_locked(self, exc: Exception) -> None:
        if self.active_request is None:
            return
        if not self.active_request.response_future.done():
            self.active_request.response_future.set_exception(exc)
        self.active_request = None
        self._active_request_ready = anyio.Event()

    @staticmethod
    def _wait_or_kill_process(process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


class HarnessSessionRegistry:
    def __init__(self, *, workdir: str, launcher: OpencodeHarnessLauncher) -> None:
        self.workdir = str(Path(workdir).resolve())
        self.launcher = launcher
        self.lock = anyio.Lock()
        self.sessions: dict[str, HarnessSessionBridge] = {}

    async def on_initialize(self, session_id: str) -> None:
        session = await self.ensure_session(session_id)
        await session.on_initialize()

    async def close_session(self, session_id: str) -> None:
        async with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def ensure_session(self, session_id: str) -> HarnessSessionBridge:
        async with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = HarnessSessionBridge(session_id=session_id, workdir=self.workdir, launcher=self.launcher)
                self.sessions[session_id] = session
            return session

    async def ensure_tools_ready(self, session_id: str, timeout_seconds: float = HIJACK_CONNECT_TIMEOUT_SECONDS) -> list[Any]:
        session = await self.ensure_session(session_id)
        return await session.ensure_tools_ready(timeout_seconds)

    async def call_tool(self, session_id: str, name: str, arguments: dict[str, Any]) -> str:
        session = await self.ensure_session(session_id)
        return await session.call_tool(name, arguments)

    async def on_hijack_request(self, session_id: str, body: dict[str, Any], model: str, stream: bool) -> ActiveHijackRequest:
        session = await self.ensure_session(session_id)
        return await session.on_hijack_request(body, model, stream)

    async def release_hijack_request(self, session_id: str, active_request: ActiveHijackRequest) -> None:
        async with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            return
        await session.release_hijack_request(active_request)
