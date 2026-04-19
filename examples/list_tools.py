from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_MCP_URL = "http://127.0.0.1:9330/mcp"
DEFAULT_PREVIEW_LENGTH = 500


def _parse_args(argv: list[str]) -> tuple[str, bool]:
    mcp_url = None
    full = False
    for arg in argv[1:]:
        if arg in {"--full", "-f"}:
            full = True
            continue
        if mcp_url is None:
            mcp_url = arg
    return mcp_url or os.environ.get("MCP_URL") or DEFAULT_MCP_URL, full


def _tool_name(tool: Any) -> str:
    if hasattr(tool, "name"):
        return str(tool.name)
    if isinstance(tool, dict):
        return str(tool.get("name", "<unnamed>"))
    return str(getattr(tool, "name", "<unnamed>"))


def _tool_details(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        return tool.model_dump()
    if isinstance(tool, dict):
        return tool
    return {key: value for key, value in vars(tool).items() if not key.startswith("_")}


def _compact_preview(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= DEFAULT_PREVIEW_LENGTH:
        return text
    return f"{text[:DEFAULT_PREVIEW_LENGTH]}..."


async def _run(argv: list[str]) -> int:
    mcp_url, full = _parse_args(argv)
    print(f"Connecting to MCP server: {mcp_url}")
    print("Tip: pass a URL as the first argument, set MCP_URL, or use --full for full output.")

    try:
        async with streamablehttp_client(mcp_url) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                init_result = await session.initialize()
                print(f"  MCP initialized: protocol={init_result.protocolVersion}")
                print("\ninitialize.result.instructions:")
                if full:
                    print(init_result.instructions or "<empty>")
                else:
                    print(_compact_preview(init_result.instructions or "<empty>"))

                tools_result = await session.list_tools()
                tools = list(tools_result.tools)
                print(f"\nDiscovered {len(tools)} tool(s):")
                for index, tool in enumerate(tools, start=1):
                    print(f"  {index:>2}. {_tool_name(tool)}")

                if not tools:
                    print("\nNo tools were returned by this MCP server.")
                    return 2

                print("\nFirst tool details:")
                tool_details = _tool_details(tools[0])
                if full:
                    print(json.dumps(tool_details, indent=2, ensure_ascii=False, default=str))
                else:
                    print(_compact_preview(tool_details))
    except Exception as exc:
        print(f"\nFailed to list tools from {mcp_url}: {exc}")
        print("Make sure harness_to_mcp is already running and the MCP URL is correct.")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(__import__("asyncio").run(_run(sys.argv)))
