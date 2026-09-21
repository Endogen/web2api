"""Tests for stable MCP tool naming."""

from web2api.mcp_utils import build_tool_name, parse_tool_name


def test_tool_names_round_trip_when_slug_contains_underscores() -> None:
    name = build_tool_name("my_recipe", "search_items")

    assert name == "my_recipe__search_items"
    assert parse_tool_name(name) == ("my_recipe", "search_items")
