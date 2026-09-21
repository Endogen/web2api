"""Unit tests for MCP protocol tool generation."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from web2api.config import RecipeConfig
from web2api.mcp_server import _ToolRegistry
from web2api.registry import Recipe


class FakeRegistry:
    """Minimal registry stub for MCP tool generation tests."""

    def __init__(self, recipe: Recipe) -> None:
        self._recipe = recipe

    def list_all(self) -> list[Recipe]:
        return [self._recipe]

    def get(self, slug: str) -> Recipe | None:
        return self._recipe if slug == self._recipe.config.slug else None


class FakeMCP:
    """Minimal FastMCP stub that captures registered tool functions."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):  # noqa: ANN201
        _ = description

        def decorator(fn):  # noqa: ANN001, ANN202
            self.tools[name] = fn
            return fn

        return decorator

    def remove_tool(self, name: str) -> None:
        self.tools.pop(name, None)


def test_mcp_tool_signature_marks_required_extra_params_as_required() -> None:
    config = RecipeConfig.model_validate(
        {
            "name": "Demo",
            "slug": "demo",
            "base_url": "https://example.com",
            "description": "Fixture recipe",
            "endpoints": {
                "read": {
                    "url": "https://example.com/items?page={page}",
                    "params": {
                        "token": {
                            "description": "Required token",
                            "required": True,
                        }
                    },
                    "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
                    "pagination": {"type": "page_param", "param": "page"},
                }
            },
        }
    )
    recipe = Recipe(config=config, scraper=None, path=Path("recipes/demo"))
    registry = FakeRegistry(recipe)
    app = SimpleNamespace(state=SimpleNamespace(registry=registry))
    mcp = FakeMCP()

    tool_registry = _ToolRegistry(mcp, app=app, bootstrap_registry=registry)
    tool_registry.build_tools()

    tool_fn = mcp.tools["demo__read"]
    signature = tool_fn.__signature__
    params = list(signature.parameters.values())

    assert params[0].name == "token"
    assert params[0].kind == inspect.Parameter.KEYWORD_ONLY
    assert params[0].default is inspect.Signature.empty
    assert params[1].name == "q"
    assert params[1].kind == inspect.Parameter.KEYWORD_ONLY
    assert params[1].default == ""
    assert params[2].name == "page"
    assert params[2].annotation is int
    assert params[2].default == 1
