from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from harness_to_mcp.server import MCPAcceptCompatibilityMiddleware


async def echo_accept_header(request: Request) -> JSONResponse:
    return JSONResponse({"accept": request.headers.get("accept")})


def build_test_client() -> TestClient:
    app = Starlette(
        routes=[
            Route("/mcp", echo_accept_header, methods=["POST"]),
            Route("/harness_to_mcp/mcp", echo_accept_header, methods=["POST"]),
            Route("/other", echo_accept_header, methods=["POST"]),
        ]
    )
    app.add_middleware(MCPAcceptCompatibilityMiddleware)
    return TestClient(app)


def test_middleware_normalizes_primary_mcp_path() -> None:
    with build_test_client() as client:
        response = client.post("/mcp", headers={"accept": "*/*"})
    assert response.status_code == 200
    assert response.json()["accept"] == "*/*, application/json"


def test_middleware_normalizes_prefixed_mcp_path() -> None:
    with build_test_client() as client:
        response = client.post("/harness_to_mcp/mcp", headers={"accept": "*/*"})
    assert response.status_code == 200
    assert response.json()["accept"] == "*/*, application/json"


def test_middleware_does_not_touch_other_paths() -> None:
    with build_test_client() as client:
        response = client.post("/other", headers={"accept": "*/*"})
    assert response.status_code == 200
    assert response.json()["accept"] == "*/*"
