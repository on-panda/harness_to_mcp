from __future__ import annotations

import asyncio
import argparse
import contextlib
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import uuid4

import anyio
import uvicorn
from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    Request,
    Scope,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .__info__ import __description__, __version__
from .adapters import (
    HIJACK_MODEL_ID,
    ApiAdapter,
    OpenAIResponsesAdapter,
    TurnPayload,
    adapter_routes,
    build_adapters,
    tool_result_to_mcp_content,
)
from .bridge import HIJACK_CONNECT_TIMEOUT_SECONDS, HarnessSessionRegistry
from .launchers import HarnessLauncher, build_launchers

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9330
DEFAULT_HEARTBEAT_SECONDS = 50
SERVER_PROBE_TIMEOUT_SECONDS = 1.5
CORS_EXPOSE_HEADERS = ["mcp-session-id", "mcp-protocol-version"]
MCP_PATHS = ("/mcp", "/harness_to_mcp/mcp")
MODELS_PATH = "/harness_to_mcp/v1/models"
HEALTH_PATH = "/harness_to_mcp/health"


def _enable_default_logging() -> None:
    package_logger = logging.getLogger("harness_to_mcp")
    mcp_request_logger = logging.getLogger("mcp.server.lowlevel.server")
    if package_logger.level == logging.NOTSET:
        package_logger.setLevel(logging.INFO)
    if mcp_request_logger.level == logging.NOTSET:
        mcp_request_logger.setLevel(logging.WARNING)
    if logging.getLogger().handlers:
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class MCPAcceptCompatibilityMiddleware:
    def __init__(self, app, paths: tuple[str, ...] = MCP_PATHS) -> None:
        self.app = app
        self.paths = set(paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] in self.paths:
            headers = MutableHeaders(scope=scope)
            accept = headers.get("accept")
            if not accept:
                headers["accept"] = "application/json"
            elif "*/*" in accept and "application/json" not in accept:
                headers["accept"] = f"{accept}, application/json"
        await self.app(scope, receive, send)


class HarnessTransport(StreamableHTTPServerTransport):
    def __init__(self, *, session_id: str, registry: HarnessSessionRegistry, **kwargs: Any) -> None:
        super().__init__(mcp_session_id=session_id, **kwargs)
        self.registry = registry

    async def _handle_post_request(self, scope: Scope, request: Request, receive, send) -> None:
        body = await request.body()
        with contextlib.suppress(json.JSONDecodeError):
            message = json.loads(body)
            if isinstance(message, dict) and message.get("method") == "initialize":
                await self.registry.on_initialize(self.mcp_session_id)
        await super()._handle_post_request(scope, request, receive, send)

    async def terminate(self) -> None:
        await super().terminate()
        if self.mcp_session_id is not None:
            await self.registry.close_session(self.mcp_session_id)


class HarnessSessionManager(StreamableHTTPSessionManager):
    def __init__(
        self,
        *,
        app: Server[Any, Any],
        registry: HarnessSessionRegistry,
        json_response: bool,
        pinned_session_id: str | None = None,
    ) -> None:
        super().__init__(app=app, json_response=json_response, stateless=False)
        self.registry = registry
        self.pinned_session_id = pinned_session_id

    async def _handle_stateful_request(self, scope: Scope, receive, send) -> None:
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        transport = self._server_instances.get(request_mcp_session_id) if request_mcp_session_id is not None else None
        if transport is None:
            transport = await self._start_transport(request_mcp_session_id)
        await transport.handle_request(scope, receive, send)

    async def _start_transport(self, requested_session_id: str | None) -> HarnessTransport:
        async with self._session_creation_lock:
            restored_session = requested_session_id is not None
            if requested_session_id is not None:
                existing = self._server_instances.get(requested_session_id)
                if existing is not None:
                    return existing
                session_id = requested_session_id
                logger.info("Restored MCP session in resume mode")
            else:
                session_id = self.pinned_session_id or uuid4().hex
                if session_id in self._server_instances:
                    session_id = uuid4().hex

            transport = HarnessTransport(
                session_id=session_id,
                registry=self.registry,
                is_json_response_enabled=self.json_response,
                event_store=self.event_store,
                security_settings=self.security_settings,
                retry_interval=self.retry_interval,
            )
            self._server_instances[session_id] = transport
            initialization_options = await _session_initialization_options(
                self.app,
                self.registry,
                session_id,
                wait_for_tools=not restored_session,
            )

            async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED) -> None:
                async with transport.connect() as streams:
                    read_stream, write_stream = streams
                    task_status.started()
                    try:
                        await self.app.run(
                            read_stream,
                            write_stream,
                            initialization_options,
                            stateless=restored_session,
                        )
                    finally:
                        self._server_instances.pop(session_id, None)
                        await self.registry.close_session(session_id)

            assert self._task_group is not None
            await self._task_group.start(run_server)
            return transport


@dataclass(slots=True)
class AppState:
    registry: HarnessSessionRegistry
    adapters: dict[str, ApiAdapter]
    launchers: dict[str, HarnessLauncher]
    helper_harness_name: str | None
    heartbeat_seconds: int


class HarnessToMcp:
    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        workdir: str | None = None,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
        helper_harness_name: str | None = None,
        pinned_session_id: str | None = None,
        launchers: dict[str, HarnessLauncher] | None = None,
    ) -> None:
        self.host = host
        self.port = _pick_port(port)
        self.workdir = os.path.abspath(workdir or os.getcwd())
        self.heartbeat_seconds = heartbeat_seconds
        self.helper_harness_name = helper_harness_name
        self.pinned_session_id = pinned_session_id
        self.launchers = launchers
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None

    @property
    def base_url(self) -> str:
        return f"http://{_connect_host(self.host)}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def hijack_root_url(self) -> str:
        return f"{self.base_url}/harness_to_mcp"

    @property
    def hijack_base_url(self) -> str:
        return f"{self.hijack_root_url}/v1"

    @property
    def anthropic_base_url(self) -> str:
        return self.hijack_root_url

    def start(self) -> None:
        if self._thread is not None:
            return
        _enable_default_logging()
        app = create_app(
            host=self.host,
            port=self.port,
            workdir=self.workdir,
            heartbeat_seconds=self.heartbeat_seconds,
            helper_harness_name=self.helper_harness_name,
            pinned_session_id=self.pinned_session_id,
            launchers=self.launchers,
        )
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="info")
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(target=server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not server.started:
            if time.time() >= deadline:
                raise RuntimeError("Timed out while starting harness_to_mcp server.")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is None or self._thread is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._server = None
        self._thread = None

    def __enter__(self) -> HarnessToMcp:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness_to_mcp", description=__description__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workdir", default=os.getcwd())
    subparsers = parser.add_subparsers(dest="subcommand")

    launchers = build_launchers()
    for name in sorted(launchers):
        sub = subparsers.add_parser(name, help=f"Launch {name} against the hijack API server.")
        sub.add_argument("--host", default=DEFAULT_HOST)
        sub.add_argument("--port", type=int, default=DEFAULT_PORT)
        sub.add_argument("--session-token")
        sub.add_argument("--prompt", default=_launch_prompt_for(launchers[name]))
        sub.add_argument("--workdir", default=os.getcwd())
    return parser


def create_app(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workdir: str | None = None,
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    helper_harness_name: str | None = None,
    pinned_session_id: str | None = None,
    launchers: dict[str, HarnessLauncher] | None = None,
) -> Starlette:
    connect_host = _connect_host(host)
    base_url_root = f"http://{connect_host}:{port}/harness_to_mcp"
    launchers = launchers or build_launchers()
    if helper_harness_name is not None and helper_harness_name not in launchers:
        raise ValueError(f"Unknown helper harness: {helper_harness_name}")
    adapters = build_adapters()
    registry = HarnessSessionRegistry(
        workdir=workdir or os.getcwd(),
        base_url_root=base_url_root,
        launchers=launchers,
        default_launcher_name=helper_harness_name,
    )
    mcp_server = _build_mcp_server(registry)
    state = AppState(
        registry=registry,
        adapters=adapters,
        launchers=launchers,
        helper_harness_name=helper_harness_name,
        heartbeat_seconds=heartbeat_seconds,
    )
    session_manager = HarnessSessionManager(
        app=mcp_server,
        registry=registry,
        json_response=True,
        pinned_session_id=pinned_session_id,
    )
    mcp_http_app = StreamableHTTPASGIApp(session_manager)

    routes = [
        Route("/mcp", endpoint=mcp_http_app, methods=["GET", "POST", "DELETE"]),
        Route("/harness_to_mcp/mcp", endpoint=mcp_http_app, methods=["GET", "POST", "DELETE"]),
        Route(MODELS_PATH, endpoint=_models_endpoint, methods=["GET"]),
        Route(HEALTH_PATH, endpoint=_health_endpoint, methods=["GET"]),
    ]
    for path, adapter in adapter_routes(adapters).items():
        routes.append(Route(path, endpoint=_make_hijack_endpoint(state, adapter), methods=["POST"]))
        if isinstance(adapter, OpenAIResponsesAdapter):
            routes.append(WebSocketRoute(path, endpoint=_make_responses_websocket_endpoint(state, adapter)))

    app = Starlette(routes=routes)
    app.state.harness_to_mcp = state
    app.add_middleware(MCPAcceptCompatibilityMiddleware, paths=MCP_PATHS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=CORS_EXPOSE_HEADERS,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with session_manager.run():
            try:
                yield
            finally:
                await registry.close()

    app.router.lifespan_context = lifespan
    return app


def main(argv: Sequence[str] | None = None) -> int:
    _enable_default_logging()
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    launchers = build_launchers()
    if args.subcommand in launchers:
        return _run_launcher_command(launchers[args.subcommand], args, launchers)
    port = _pick_port(args.port)
    uvicorn.run(
        create_app(
            host=args.host,
            port=port,
            workdir=args.workdir,
        ),
        host=args.host,
        port=port,
    )
    return 0


def _run_launcher_command(launcher: HarnessLauncher, args: argparse.Namespace, launchers: dict[str, HarnessLauncher] | None = None) -> int:
    session_token = args.session_token or uuid4().hex
    server = HarnessToMcp(
        host=args.host,
        port=args.port,
        workdir=args.workdir,
        helper_harness_name=launcher.name,
        pinned_session_id=session_token,
        launchers=launchers,
    )
    server.start()
    runtime, process = launcher.create_process(
        base_url_root=server.hijack_root_url,
        session_token=session_token,
        prompt=args.prompt or _launch_prompt_for(launcher),
        workdir=args.workdir,
    )
    try:
        while server._thread is not None and server._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        if process.poll() is None:
            process.terminate()
        runtime.cleanup()
        launcher.shutdown()
    return 0


async def _models_endpoint(_: StarletteRequest) -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": HIJACK_MODEL_ID, "object": "model", "created": int(time.time()), "owned_by": "harness_to_mcp"}],
        }
    )


async def _health_endpoint(request: StarletteRequest) -> JSONResponse:
    state: AppState = request.app.state.harness_to_mcp
    return JSONResponse(
        {
            "ok": True,
            "helper_harness": state.helper_harness_name,
            "launchers": sorted(state.launchers),
            "adapters": sorted(state.adapters),
        }
    )


def _make_hijack_endpoint(state: AppState, adapter: ApiAdapter):
    async def endpoint(request: StarletteRequest):
        session_id = adapter.session_token_from_headers(request.headers)
        if not session_id:
            return JSONResponse(adapter.error_body("Missing harness session token."), status_code=401)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(adapter.error_body("Invalid JSON request body."), status_code=400)
        hijack_request = adapter.parse_request(body)
        if not adapter.request_has_tools(body):
            payload = TurnPayload(model=hijack_request.model, text=adapter.default_text_response(body))
            if hijack_request.stream:
                return StreamingResponse(
                    _iter_static_chunks(adapter.build_stream_chunks(payload)),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
            return JSONResponse(adapter.build_json_response(payload))
        if not hijack_request.stream and hijack_request.tool_results:
            return JSONResponse(adapter.build_json_response(TurnPayload(model=hijack_request.model, text="ok")))
        active_request = await state.registry.on_hijack_request(
            session_id,
            adapter_name=adapter.name,
            request=hijack_request,
        )
        if hijack_request.stream:
            return StreamingResponse(
                _stream_hijack_response(
                    registry=state.registry,
                    session_id=session_id,
                    adapter=adapter,
                    active_request=active_request,
                    heartbeat_seconds=state.heartbeat_seconds,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        try:
            payload = await active_request.response_future
        except RuntimeError as exc:
            await state.registry.release_hijack_request(session_id, active_request)
            return JSONResponse(adapter.error_body(str(exc)), status_code=503)
        await state.registry.release_hijack_request(session_id, active_request)
        return JSONResponse(adapter.build_json_response(payload))

    return endpoint


def _make_responses_websocket_endpoint(state: AppState, adapter: OpenAIResponsesAdapter):
    async def endpoint(websocket: WebSocket):
        session_id = adapter.session_token_from_headers(websocket.headers)
        if not session_id:
            await websocket.close(code=1008, reason="Missing harness session token.")
            return
        await websocket.accept()
        while True:
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                return
            if message.get("type") != "response.create":
                await websocket.close(code=1003, reason="Unsupported websocket message type.")
                return
            body = {key: value for key, value in message.items() if key != "type"}
            hijack_request = adapter.parse_request(body)
            if not adapter.request_has_tools(body):
                for event in adapter.build_stream_events(
                    TurnPayload(model=hijack_request.model, text=adapter.default_text_response(body))
                ):
                    await websocket.send_json(event)
                continue
            if not hijack_request.stream and hijack_request.tool_results:
                for event in adapter.build_stream_events(TurnPayload(model=hijack_request.model, text="ok")):
                    await websocket.send_json(event)
                continue
            active_request = await state.registry.on_hijack_request(
                session_id,
                adapter_name=adapter.name,
                request=hijack_request,
            )
            try:
                payload = await _wait_for_websocket_response_payload(websocket, active_request)
                if payload is None:
                    return
            except RuntimeError as exc:
                payload = TurnPayload(model=active_request.model, text=str(exc))
            finally:
                await state.registry.release_hijack_request(session_id, active_request)
            for event in adapter.build_stream_events(payload):
                await websocket.send_json(event)

    return endpoint


async def _wait_for_websocket_response_payload(websocket: WebSocket, active_request) -> TurnPayload | None:
    disconnect_task = asyncio.create_task(websocket.receive())
    done, _ = await asyncio.wait(
        {active_request.response_future, disconnect_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if active_request.response_future in done:
        disconnect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await disconnect_task
        return active_request.response_future.result()
    message = disconnect_task.result()
    if message["type"] == "websocket.disconnect":
        active_request.response_future.cancel()
        return None
    raise RuntimeError("WebSocket request was replaced before a response was ready.")


async def _stream_hijack_response(
    *,
    registry: HarnessSessionRegistry,
    session_id: str,
    adapter: ApiAdapter,
    active_request,
    heartbeat_seconds: int,
):
    try:
        while True:
            with anyio.move_on_after(heartbeat_seconds):
                payload = await active_request.response_future
                for chunk in adapter.build_stream_chunks(payload):
                    yield chunk
                return
            yield adapter.build_stream_heartbeat(active_request.model)
    except RuntimeError as exc:
        for chunk in adapter.build_stream_chunks(TurnPayload(model=active_request.model, text=str(exc))):
            yield chunk
    finally:
        await registry.release_hijack_request(session_id, active_request)


def _build_mcp_server(registry: HarnessSessionRegistry) -> Server[Any, Any]:
    server = Server("harness_to_mcp", version=__version__)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await registry.ensure_tools_ready(_current_mcp_session_id(server), HIJACK_CONNECT_TIMEOUT_SECONDS)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        output = await registry.call_tool(_current_mcp_session_id(server), name, arguments)
        return tool_result_to_mcp_content(output)

    return server


async def _session_initialization_options(
    app: Server[Any, Any],
    registry: HarnessSessionRegistry,
    session_id: str,
    *,
    wait_for_tools: bool,
) -> InitializationOptions:
    base = app.create_initialization_options()
    initial_request = await registry.get_initialize_initial_request(session_id, wait_for_tools=wait_for_tools)
    experimental = dict(base.capabilities.experimental or {})
    experimental["initialRequest"] = initial_request or {}
    return InitializationOptions(
        server_name=base.server_name,
        server_version=base.server_version,
        capabilities=base.capabilities.model_copy(update={"experimental": experimental}),
        instructions=await registry.get_initialize_instructions(session_id, wait_for_tools=wait_for_tools),
        website_url=base.website_url,
        icons=base.icons,
    )


def _current_mcp_session_id(server: Server[Any, Any]) -> str:
    request = server.request_context.request
    if request is None:
        raise RuntimeError("Missing MCP request context.")
    session_id = request.headers.get(MCP_SESSION_ID_HEADER)
    if not session_id:
        raise RuntimeError("Missing MCP session header.")
    return session_id


def _iter_static_chunks(chunks: list[bytes]):
    async def generator():
        for chunk in chunks:
            yield chunk

    return generator()


def _pick_port(port: int) -> int:
    if port != 0:
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        return sock.getsockname()[1]


def _connect_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _is_local_host(host: str) -> bool:
    return host in {"127.0.0.1", "0.0.0.0", "::", "localhost"}


def _server_is_ready(base_url: str) -> bool:
    models_url = f"{base_url.rstrip('/')}/models" if base_url.rstrip("/").endswith("/v1") else f"{base_url.rstrip('/')}/harness_to_mcp/v1/models"
    request = urllib.request.Request(models_url, method="GET", headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=SERVER_PROBE_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False
    data = body.get("data")
    return isinstance(data, list) and any(item.get("id") == HIJACK_MODEL_ID for item in data if isinstance(item, dict))


_hijack_server_is_ready = _server_is_ready


def _launch_prompt_for(launcher: HarnessLauncher) -> str:
    from .launchers import LAUNCH_PROMPT

    return LAUNCH_PROMPT
