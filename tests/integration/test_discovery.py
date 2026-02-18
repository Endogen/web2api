"""Integration tests for recipe discovery."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from web2api.config import RecipeConfig
from web2api.registry import RecipeRegistry


def _read_endpoint(url: str) -> dict[str, object]:
    return {
        "url": url,
        "items": {
            "container": ".item",
            "fields": {
                "title": {"selector": ".title"},
                "url": {"selector": "a", "attribute": "href"},
            },
        },
        "pagination": {"type": "page_param", "param": "page"},
    }


def _write_recipe(recipe_dir: Path, *, slug: str | None = None) -> None:
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe_slug = slug or recipe_dir.name
    payload = {
        "name": f"{recipe_slug} site",
        "slug": recipe_slug,
        "base_url": "https://example.com",
        "description": "Fixture recipe for discovery tests.",
        "capabilities": ["read"],
        "endpoints": {
            "read": _read_endpoint("https://example.com/items?page={page}"),
        },
    }
    (recipe_dir / "recipe.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_discovery_loads_valid_recipe(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir / "valid")

    registry = RecipeRegistry()
    registry.discover(recipes_dir)

    assert registry.count == 1
    recipe = registry.get("valid")
    assert recipe is not None
    assert recipe.path == recipes_dir / "valid"
    assert recipe.scraper is None


def test_discovery_skips_invalid_recipe(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir / "valid")
    broken_dir = recipes_dir / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "recipe.yaml").write_text("name: Broken\nslug: broken\n", encoding="utf-8")

    registry = RecipeRegistry()
    with caplog.at_level(logging.WARNING):
        registry.discover(recipes_dir)

    assert registry.count == 1
    assert registry.get("valid") is not None
    assert registry.get("broken") is None
    assert any("Skipping invalid recipe 'broken'" in message for message in caplog.messages)


def test_discovery_handles_empty_directory(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir(parents=True)

    registry = RecipeRegistry()
    registry.discover(recipes_dir)

    assert registry.count == 0
    assert registry.list_all() == []


def test_discovery_warns_and_skips_duplicate_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir / "first", slug="dup")
    _write_recipe(recipes_dir / "second", slug="dup")

    def _parse_without_folder_match(
        data: dict[str, object], folder_name: str | None = None
    ) -> RecipeConfig:
        _ = folder_name
        return RecipeConfig.model_validate(data)

    monkeypatch.setattr("web2api.registry.parse_recipe_config", _parse_without_folder_match)

    registry = RecipeRegistry()
    with caplog.at_level(logging.WARNING):
        registry.discover(recipes_dir)

    assert registry.count == 1
    assert registry.get("dup") is not None
    assert any("duplicate slug 'dup'" in message for message in caplog.messages)


def test_discovery_loads_custom_scraper(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    custom_dir = recipes_dir / "custom"
    _write_recipe(custom_dir)
    (custom_dir / "scraper.py").write_text(
        "\n".join(
            [
                "from web2api.scraper import BaseScraper, ScrapeResult",
                "",
                "class Scraper(BaseScraper):",
                "    async def read(self, page, params):",
                "        return ScrapeResult()",
            ]
        ),
        encoding="utf-8",
    )

    registry = RecipeRegistry()
    registry.discover(recipes_dir)

    recipe = registry.get("custom")
    assert recipe is not None
    assert recipe.scraper is not None
    assert recipe.scraper.supports_read() is True
    assert recipe.scraper.supports_search() is False
