"""Tests for stable MCP tool naming."""

from web2api.mcp_utils import build_tool_name


def test_tool_name_uses_double_underscore_separator() -> None:
    assert build_tool_name("my_recipe", "search_items") == "my_recipe__search_items"


def test_tool_name_override_takes_precedence() -> None:
    assert build_tool_name("demo", "read", override="read_webpage") == "read_webpage"
