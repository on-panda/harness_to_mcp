from harness_to_mcp import __description__
from harness_to_mcp.__info__ import __author__, __url__


def test_metadata_values() -> None:
    assert "MCP" in __description__
    assert __author__ == "DIYer22"
    assert __url__ == "https://github.com/on-panda/harness_to_mcp"
