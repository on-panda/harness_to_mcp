from harness_to_mcp import __description__, __version__
from harness_to_mcp.__info__ import __author__, __url__


def test_metadata_values() -> None:
    assert __version__ == "0.1.0"
    assert "MCP" in __description__
    assert __author__ == "DIYer22"
    assert __url__ == "https://github.com/on-panda/harness_to_mcp"
