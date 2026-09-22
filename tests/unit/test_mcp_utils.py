"""Tests for canonical MCP tool definitions and arguments."""

import pytest

from web2api.config import EndpointConfig
from web2api.mcp_utils import (
    build_tool_name,
    build_tool_parameters,
    normalize_tool_arguments,
)


def test_tool_name_uses_double_underscore_separator() -> None:
    assert build_tool_name("my_recipe", "search_items") == "my_recipe__search_items"


def test_tool_name_override_takes_precedence() -> None:
    assert build_tool_name("demo", "read", override="read_webpage") == "read_webpage"


def test_tool_arguments_reject_conflicting_query_aliases() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        normalize_tool_arguments({"q": "one", "query": "two"})


def test_tool_arguments_reject_non_scalar_query_values_cleanly() -> None:
    with pytest.raises(ValueError, match="q must be a string"):
        normalize_tool_arguments({"q": ["one"], "page": 2})


def test_optional_typed_parameters_do_not_advertise_invalid_empty_defaults() -> None:
    endpoint = EndpointConfig.model_validate(
        {
            "url": "https://example.com/items",
            "params": {"limit": {"type": "integer", "minimum": 1}},
            "items": {
                "container": ".item",
                "fields": {"title": {"selector": ".title"}},
            },
            "pagination": {"type": "page_param", "param": "page"},
        }
    )

    schema = build_tool_parameters(endpoint)
    assert schema["properties"]["limit"] == {"type": "integer", "minimum": 1.0}
    assert "limit" not in schema.get("required", [])
