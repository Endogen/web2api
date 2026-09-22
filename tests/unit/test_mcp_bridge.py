"""Unit tests for MCP HTTP bridge tool resolution."""

from __future__ import annotations

from pathlib import Path

from web2api.config import RecipeConfig
from web2api.mcp_utils import resolve_tool_spec
from web2api.registry import Recipe


class _FakeRegistry:
    """Minimal registry stub exposing ``list_all``."""

    def __init__(self, recipes: list[Recipe]) -> None:
        self._recipes = recipes

    def list_all(self) -> list[Recipe]:
        return self._recipes


def _endpoint(**overrides: object) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "url": "https://example.com/items?page={page}",
        "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
        "pagination": {"type": "page_param", "param": "page"},
    }
    endpoint.update(overrides)
    return endpoint


def _recipe(slug: str, endpoints: dict[str, object]) -> Recipe:
    config = RecipeConfig.model_validate(
        {
            "name": slug.title(),
            "slug": slug,
            "base_url": "https://example.com",
            "description": "fixture recipe",
            "endpoints": endpoints,
        }
    )
    return Recipe(config=config, scraper=None, path=Path(f"recipes/{slug}"))


def _resolved_target(registry: _FakeRegistry, tool_name: str) -> tuple[str, str] | None:
    spec = resolve_tool_spec(registry, tool_name)
    return None if spec is None else (spec.slug, spec.endpoint)


def test_resolve_tool_standard_naming() -> None:
    registry = _FakeRegistry([_recipe("demo", {"read": _endpoint()})])

    assert _resolved_target(registry, "demo__read") == ("demo", "read")


def test_resolve_tool_legacy_single_underscore_name() -> None:
    registry = _FakeRegistry([_recipe("demo", {"read": _endpoint()})])

    assert _resolved_target(registry, "demo_read") == ("demo", "read")


def test_resolve_tool_handles_underscores_in_slug_and_endpoint() -> None:
    registry = _FakeRegistry([_recipe("my_site", {"deep_read": _endpoint()})])

    assert _resolved_target(registry, "my_site__deep_read") == ("my_site", "deep_read")


def test_resolve_tool_handles_double_underscores_in_slug() -> None:
    registry = _FakeRegistry([_recipe("my__site", {"read": _endpoint()})])

    # Splitting on "__" would mis-parse this as ("my", "site__read").
    assert _resolved_target(registry, "my__site__read") == ("my__site", "read")


def test_resolve_tool_custom_tool_name_override() -> None:
    registry = _FakeRegistry([_recipe("allenai", {"molmo2": _endpoint(tool_name="molmo2_vision")})])

    assert _resolved_target(registry, "molmo2_vision") == ("allenai", "molmo2")


def test_resolve_tool_unknown_name_returns_none() -> None:
    registry = _FakeRegistry([_recipe("demo", {"read": _endpoint()})])

    assert _resolved_target(registry, "missing_tool") is None
