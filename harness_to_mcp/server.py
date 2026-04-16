from __future__ import annotations

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
from http import HTTPStatus
from typing import Any, Sequence
from uuid import uuid4

import anyio
import uvicorn
from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.streamable_http import (
    CONTENT_TYPE_JSON,
    ErrorData,
    INVALID_REQUEST,
    JSONRPCError,
    MCP_SESSION_ID_HEADER,
    Request,
    Response,
    Scope,
    Send,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.applications import Starlette

from .__info__ import __description__, __version__
from .bridge import HIJACK_CONNECT_TIMEOUT_SECONDS, HarnessSessionRegistry, OpencodeHarnessLauncher
from .openai_chat import (
    HIJACK_MODEL_ID,
    CompletionPayload,
    build_json_response,
    build_stream_chunks,
    build_stream_heartbeat,
    default_text_response,
    openai_error,
    request_has_tools,
)
from .opencode import LAUNCH_PROMPT, run_opencode

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9330
DEFAULT_HEARTBEAT_SECONDS = 600
OPENCODE_PROBE_TIMEOUT_SECONDS = 1.5
CORS_EXPOSE_HEADERS = ["mcp-session-id", "mcp-protocol-version"]
MCP_PATHS = ("/mcp", "/harness_to_mcp/mcp")
CHAT_COMPLETIONS_PATH = "/harness_to_mcp/v1/chat/completions"
MODELS_PATH = "/harness_to_mcp/v1/models"


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
    def __init__(self, *, app: Server[Any, Any], registry: HarnessSessionRegistry, json_response: bool) -> None:
        super().__init__(app=app, json_response=json_response, stateless=False)
        self.registry = registry

    async def _handle_stateful_request(self, scope: Scope, receive, send) -> None:
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        if request_mcp_session_id is not None and request_mcp_session_id in self._server_instances:
            transport = self._server_instances[request_mcp_session_id]
            await transport.handle_request(scope, receive, send)
            return

        if request_mcp_session_id is None:
            async with self._session_creation_lock:
                new_session_id = uuid4().hex
                transport = HarnessTransport(
                    session_id=new_session_id,
                    registry=self.registry,
                    is_json_response_enabled=self.json_response,
                    event_store=self.event_store,
                    security_settings=self.security_settings,
                    retry_interval=self.retry_interval,
                )
                self._server_instances[new_session_id] = transport

                async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED) -> None:
                    async with transport.connect() as streams:
                        read_stream, write_stream = streams
                        task_status.started()
                        try:
                            await self.app.run(
                                read_stream,
                                write_stream,
                                self.app.create_initialization_options(),
                                stateless=False,
                            )
                        finally:
                            self._server_instances.pop(new_session_id, None)
                            await self.registry.close_session(new_session_id)

                assert self._task_group is not None
                await self._task_group.start(run_server)
                await transport.handle_request(scope, receive, send)
                return

        error_response = JSONRPCError(
            jsonrpc="2.0",
            id="server-error",
            error=ErrorData(code=INVALID_REQUEST, message="Session not found"),
        )
        response = Response(
            content=error_response.model_dump_json(by_alias=True, exclude_none=True),
            status_code=HTTPStatus.NOT_FOUND,
            media_type=CONTENT_TYPE_JSON,
        )
        await response(scope, receive, send)


@dataclass(slots=True)
class AppState:
    registry: HarnessSessionRegistry
    mcp_server: Server[Any, Any]
    heartbeat_seconds: int


class HarnessToMcp:
    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        workdir: str | None = None,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        self.host = host
        self.port = _pick_port(port)
        self.workdir = os.path.abspath(workdir or os.getcwd())
        self.heartbeat_seconds = heartbeat_seconds
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None

    @property
    def base_url(self) -> str:
        return f"http://{_connect_host(self.host)}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def hijack_base_url(self) -> str:
        return f"{self.base_url}/harness_to_mcp/v1"

    def start(self) -> None:
        if self._thread is not None:
            return
        app = create_app(
            host=self.host,
            port=self.port,
            workdir=self.workdir,
            heartbeat_seconds=self.heartbeat_seconds,
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
    parser = argparse.ArgumentParser(
        prog="harness_to_mcp",
        description=__description__,
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workdir", default=os.getcwd())
    subparsers = parser.add_subparsers(dest="subcommand")

    opencode_parser = subparsers.add_parser("opencode", help="Launch opencode against the hijack API server.")
    opencode_parser.add_argument("--host", default=DEFAULT_HOST)
    opencode_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    opencode_parser.add_argument("--session-token")
    opencode_parser.add_argument("--prompt", default=LAUNCH_PROMPT)
    opencode_parser.add_argument("--workdir", default=os.getcwd())
    return parser


def create_app(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    workdir: str | None = None,
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
) -> Starlette:
    connect_host = _connect_host(host)
    base_url = f"http://{connect_host}:{port}/harness_to_mcp/v1"
    registry = HarnessSessionRegistry(
        workdir=workdir or os.getcwd(),
        launcher=OpencodeHarnessLauncher(base_url=base_url, prompt=LAUNCH_PROMPT),
    )
    mcp_server = _build_mcp_server(registry)
    state = AppState(registry=registry, mcp_server=mcp_server, heartbeat_seconds=heartbeat_seconds)
    session_manager = HarnessSessionManager(app=mcp_server, registry=registry, json_response=True)
    mcp_http_app = StreamableHTTPASGIApp(session_manager)

    async def models_endpoint(_: StarletteRequest) -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": HIJACK_MODEL_ID,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "harness_to_mcp",
                    }
                ],
            }
        )

    async def chat_completions_endpoint(request: StarletteRequest):
        session_id = _extract_bearer_token(request.headers.get("authorization"))
        if not session_id:
            return JSONResponse(openai_error("Missing bearer token for harness session."), status_code=401)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(openai_error("Invalid JSON request body."), status_code=400)

        model = body.get("model") or HIJACK_MODEL_ID
        stream = bool(body.get("stream"))
        if not request_has_tools(body):
            payload = CompletionPayload(model=model, text=default_text_response(body))
            if stream:
                return StreamingResponse(
                    _iter_static_chunks(build_stream_chunks(payload)),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
            return JSONResponse(build_json_response(payload))

        active_request = await state.registry.on_hijack_request(session_id, body, model, stream)
        if stream:
            return StreamingResponse(
                _stream_hijack_response(
                    registry=state.registry,
                    session_id=session_id,
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
            return JSONResponse(openai_error(str(exc)), status_code=503)
        await state.registry.release_hijack_request(session_id, active_request)
        return JSONResponse(build_json_response(payload))

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=mcp_http_app, methods=["GET", "POST", "DELETE"]),
            Route("/harness_to_mcp/mcp", endpoint=mcp_http_app, methods=["GET", "POST", "DELETE"]),
            Route(MODELS_PATH, endpoint=models_endpoint, methods=["GET"]),
            Route(CHAT_COMPLETIONS_PATH, endpoint=chat_completions_endpoint, methods=["POST"]),
        ],
    )
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
            yield

    app.router.lifespan_context = lifespan
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "opencode":
        if args.port == 0:
            with HarnessToMcp(host=args.host, port=0, workdir=args.workdir) as server:
                return run_opencode(
                    base_url=server.hijack_base_url,
                    session_token=args.session_token,
                    prompt=args.prompt,
                    workdir=args.workdir,
                )
        base_url = f"http://{_connect_host(args.host)}:{args.port}/harness_to_mcp/v1"
        if _is_local_host(args.host) and not _hijack_server_is_ready(base_url):
            with HarnessToMcp(host=args.host, port=args.port, workdir=args.workdir) as server:
                return run_opencode(
                    base_url=server.hijack_base_url,
                    session_token=args.session_token,
                    prompt=args.prompt,
                    workdir=args.workdir,
                )
        return run_opencode(
            base_url=base_url,
            session_token=args.session_token,
            prompt=args.prompt,
            workdir=args.workdir,
        )
    port = _pick_port(args.port)
    uvicorn.run(
        create_app(host=args.host, port=port, workdir=args.workdir),
        host=args.host,
        port=port,
    )
    return 0


async def _stream_hijack_response(
    *,
    registry: HarnessSessionRegistry,
    session_id: str,
    active_request,
    heartbeat_seconds: int,
):
    try:
        while True:
            with anyio.move_on_after(heartbeat_seconds):
                payload = await active_request.response_future
                for chunk in build_stream_chunks(payload):
                    yield chunk
                return
            yield build_stream_heartbeat(active_request.model)
    except RuntimeError as exc:
        for chunk in build_stream_chunks(CompletionPayload(model=active_request.model, text=str(exc))):
            yield chunk
    finally:
        await registry.release_hijack_request(session_id, active_request)


def _build_mcp_server(registry: HarnessSessionRegistry) -> Server[Any, Any]:
    server = Server("harness_to_mcp", version=__version__)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        session_id = _current_mcp_session_id(server)
        return await registry.ensure_tools_ready(session_id, HIJACK_CONNECT_TIMEOUT_SECONDS)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        session_id = _current_mcp_session_id(server)
        output = await registry.call_tool(session_id, name, arguments)
        return [types.TextContent(type="text", text=output)]

    return server


def _current_mcp_session_id(server: Server[Any, Any]) -> str:
    request = server.request_context.request
    if request is None:
        raise RuntimeError("Missing MCP request context.")
    session_id = request.headers.get(MCP_SESSION_ID_HEADER)
    if not session_id:
        raise RuntimeError("Missing MCP session header.")
    return session_id


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


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


def _hijack_server_is_ready(base_url: str) -> bool:
    request = urllib.request.Request(
        f"{base_url}/models",
        method="GET",
        headers={"accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=OPENCODE_PROBE_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False
    data = body.get("data")
    return isinstance(data, list) and any(item.get("id") == HIJACK_MODEL_ID for item in data if isinstance(item, dict))
