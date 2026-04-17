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

from .adapters import HijackRequest, ToolCallSpec, TurnPayload
from .launchers import HarnessLauncher, HarnessRuntime, LAUNCH_PROMPT, launcher_for_adapter

logger = logging.getLogger(__name__)

HIJACK_CONNECT_TIMEOUT_SECONDS = 30
ACTIVE_REQUEST_GRACE_SECONDS = 2
INITIAL_EXTERNAL_HARNESS_WAIT_SECONDS = 2


@dataclass(slots=True)
class ActiveHijackRequest:
    model: str
    stream: bool
    response_future: asyncio.Future[TurnPayload]
    created_at: float


class HarnessSessionBridge:
    def __init__(
        self,
        *,
        session_id: str,
        workdir: str,
        base_url_root: str,
        launchers: dict[str, HarnessLauncher],
        default_launcher_name: str | None,
    ) -> None:
        self.session_id = session_id
        self.workdir = workdir
        self.base_url_root = base_url_root.rstrip("/")
        self.launchers = launchers
        self.launcher_name = default_launcher_name
        self.lock = anyio.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.runtime: HarnessRuntime | None = None
        self.last_harness_activity_at = 0.0
        self.tools: list[Any] = []
        self.active_request: ActiveHijackRequest | None = None
        self.pending_tool_results: dict[str, asyncio.Future[Any]] = {}
        self.mcp_open = True
        self._tools_ready = anyio.Event()
        self._active_request_ready = anyio.Event()
        self.external_harness_wait_deadline = (
            time.monotonic() + INITIAL_EXTERNAL_HARNESS_WAIT_SECONDS if default_launcher_name else 0.0
        )

    async def on_initialize(self) -> None:
        async with self.lock:
            self.mcp_open = True

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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        active_request = await self._ensure_active_request(HIJACK_CONNECT_TIMEOUT_SECONDS)
        loop = asyncio.get_running_loop()
        call_id = f"call{uuid4().hex}"
        result_future = loop.create_future()
        response = TurnPayload(
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
        logger.info("Dispatching tool call %s (%s) for session %s", call_id, name, self.session_id)
        output = await result_future
        logger.info("Tool call %s succeeded for session %s", call_id, self.session_id)
        return output

    async def on_hijack_request(self, *, adapter_name: str, request: HijackRequest) -> ActiveHijackRequest:
        loop = asyncio.get_running_loop()
        active_request = ActiveHijackRequest(
            model=request.model,
            stream=request.stream,
            response_future=loop.create_future(),
            created_at=time.monotonic(),
        )
        async with self.lock:
            inferred = launcher_for_adapter(self.launchers, adapter_name)
            if inferred is not None:
                self.launcher_name = inferred
            if self.last_harness_activity_at == 0.0:
                logger.info("Harness connected for session %s via %s", self.session_id, adapter_name)
            self.last_harness_activity_at = time.monotonic()
            for tool_result in request.tool_results:
                result_future = self.pending_tool_results.pop(tool_result.tool_call_id, None)
                if result_future is not None and not result_future.done():
                    result_future.set_result(tool_result.content)
            if request.tools:
                self.tools = request.tools
                self._tools_ready.set()
            self._clear_active_request_locked(RuntimeError("Hijack API request replaced by a newer request."))
            self.active_request = active_request
            self._active_request_ready.set()
        return active_request

    async def release_hijack_request(self, active_request: ActiveHijackRequest) -> None:
        async with self.lock:
            if self.active_request is active_request:
                logger.info("Harness disconnected for session %s", self.session_id)
                self._clear_active_request_locked(RuntimeError("Hijack API request closed."))

    async def _ensure_active_request(self, timeout_seconds: float) -> ActiveHijackRequest:
        active_request = self.active_request
        if active_request is not None:
            return active_request

        started_at = time.monotonic()
        remaining_timeout = timeout_seconds
        if self.launcher_name is not None and started_at < self.external_harness_wait_deadline:
            grace_timeout = min(remaining_timeout, self.external_harness_wait_deadline - started_at)
            event = self._active_request_ready
            with anyio.move_on_after(grace_timeout):
                await event.wait()
            active_request = self.active_request
            if active_request is not None:
                return active_request
            remaining_timeout = max(0.1, timeout_seconds - (time.monotonic() - started_at))

        now = time.monotonic()
        if self._should_restart_harness(now):
            async with self.lock:
                if self._should_restart_harness(now):
                    await self._start_harness_locked(restart=self.process is not None or self.runtime is not None)
        event = self._active_request_ready
        with anyio.fail_after(remaining_timeout):
            await event.wait()
        active_request = self.active_request
        if active_request is None:
            raise RuntimeError("Hijack API did not connect to harness within 30 seconds.")
        return active_request

    def _should_restart_harness(self, now: float) -> bool:
        if self.launcher_name is None:
            return False
        if self.process is None or self.process.poll() is not None:
            if now < self.external_harness_wait_deadline:
                return False
            return True
        if self.active_request is not None:
            return False
        return now - self.last_harness_activity_at > ACTIVE_REQUEST_GRACE_SECONDS

    async def _start_harness_locked(self, restart: bool) -> None:
        if not self.mcp_open:
            return
        if self.launcher_name is None:
            return
        if restart:
            await self._stop_harness_locked()
            self._fail_pending_locked(RuntimeError("Harness restarted before tool result arrived."))
            self._clear_active_request_locked(RuntimeError("Harness restarted."))
            self.tools = []
            self._tools_ready = anyio.Event()
        if self.process is not None and self.process.poll() is None:
            return
        launcher = self.launchers[self.launcher_name]
        self.runtime, self.process = launcher.create_process(
            base_url_root=self.base_url_root,
            session_token=self.session_id,
            prompt=LAUNCH_PROMPT,
            workdir=self.workdir,
        )
        logger.info("Started %s harness for session %s with pid %s", launcher.name, self.session_id, self.process.pid)

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
    def __init__(
        self,
        *,
        workdir: str,
        base_url_root: str,
        launchers: dict[str, HarnessLauncher],
        default_launcher_name: str | None,
    ) -> None:
        self.workdir = str(Path(workdir).resolve())
        self.base_url_root = base_url_root.rstrip("/")
        self.launchers = launchers
        self.default_launcher_name = default_launcher_name
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
                session = HarnessSessionBridge(
                    session_id=session_id,
                    workdir=self.workdir,
                    base_url_root=self.base_url_root,
                    launchers=self.launchers,
                    default_launcher_name=self.default_launcher_name,
                )
                self.sessions[session_id] = session
            return session

    async def ensure_tools_ready(self, session_id: str, timeout_seconds: float = HIJACK_CONNECT_TIMEOUT_SECONDS) -> list[Any]:
        return await (await self.ensure_session(session_id)).ensure_tools_ready(timeout_seconds)

    async def call_tool(self, session_id: str, name: str, arguments: dict[str, Any]) -> Any:
        return await (await self.ensure_session(session_id)).call_tool(name, arguments)

    async def on_hijack_request(self, session_id: str, *, adapter_name: str, request: HijackRequest) -> ActiveHijackRequest:
        return await (await self.ensure_session(session_id)).on_hijack_request(adapter_name=adapter_name, request=request)

    async def release_hijack_request(self, session_id: str, active_request: ActiveHijackRequest) -> None:
        async with self.lock:
            session = self.sessions.get(session_id)
        if session is not None:
            await session.release_hijack_request(active_request)
