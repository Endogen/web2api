"""Unit tests for plugin manager helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from web2api.plugin import parse_plugin_config
from web2api.plugin_manager import (
    DEFAULT_CATALOG_PATH,
    build_install_commands,
    copy_recipe_into_recipes_dir,
    default_recipes_dir,
    disable_recipe,
    discover_plugin_entries,
    enable_recipe,
    find_plugin_entry,
    load_catalog,
    load_manifest,
    record_plugin_install,
    remove_manifest_record,
)


def _write_recipe(recipe_dir: Path) -> None:
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "recipe.yaml").write_text(
        yaml.safe_dump(
            {
                "name": recipe_dir.name.title(),
                "slug": recipe_dir.name,
                "base_url": "https://example.com",
                "description": "fixture",
                "endpoints": {
                    "read": {
                        "url": "https://example.com/items?page={page}",
                        "items": {
                            "container": ".item",
                            "fields": {"title": {"selector": ".title"}},
                        },
                        "pagination": {"type": "page_param", "param": "page"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_discover_plugin_entries_includes_enablement_and_plugin(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    alpha_dir = recipes_dir / "alpha"
    _write_recipe(alpha_dir)
    (alpha_dir / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "1.0.0",
                "requires_env": ["ALPHA_TOKEN"],
                "dependencies": {"commands": ["bird"]},
            }
        ),
        encoding="utf-8",
    )
    disable_recipe(alpha_dir)

    entries = discover_plugin_entries(recipes_dir)
    alpha = find_plugin_entry(entries, "alpha")
    assert alpha is not None
    assert alpha.enabled is False
    assert alpha.plugin is not None
    assert alpha.plugin.requires_env == ["ALPHA_TOKEN"]

    enable_recipe(alpha_dir)
    entries_after_enable = discover_plugin_entries(recipes_dir)
    alpha_enabled = find_plugin_entry(entries_after_enable, "alpha")
    assert alpha_enabled is not None
    assert alpha_enabled.enabled is True


def test_build_install_commands_respects_include_flags() -> None:
    plugin = parse_plugin_config(
        {
            "version": "1.0.0",
            "dependencies": {
                "apt": ["nodejs"],
                "npm": ["@steipete/bird"],
                "python": ["httpx"],
            },
        }
    )

    all_commands = build_install_commands(plugin)
    assert all_commands == [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "nodejs"],
        ["npm", "install", "-g", "@steipete/bird"],
        [sys.executable, "-m", "pip", "install", "httpx"],
    ]

    python_only = build_install_commands(
        plugin,
        include_apt=False,
        include_npm=False,
        include_python=True,
    )
    assert python_only == [[sys.executable, "-m", "pip", "install", "httpx"]]


def test_manifest_record_roundtrip(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir(parents=True)

    record_plugin_install(
        recipes_dir,
        slug="alpha",
        folder="alpha",
        source_type="local",
        source="/tmp/source",
        source_ref=None,
        source_subdir="recipes/alpha",
        trusted=True,
    )

    manifest = load_manifest(recipes_dir)
    assert manifest["version"] == 1
    assert manifest["plugins"]["alpha"]["source_type"] == "local"
    assert manifest["plugins"]["alpha"]["source_subdir"] == "recipes/alpha"
    assert manifest["plugins"]["alpha"]["trusted"] is True

    removed = remove_manifest_record(recipes_dir, "alpha")
    assert removed is True
    assert load_manifest(recipes_dir)["plugins"] == {}


def test_copy_recipe_into_recipes_dir_uses_slug_folder(tmp_path: Path) -> None:
    source_recipe = tmp_path / "source-recipe"
    _write_recipe(source_recipe)
    recipe_yaml = source_recipe / "recipe.yaml"
    payload = yaml.safe_load(recipe_yaml.read_text(encoding="utf-8"))
    payload["slug"] = "renamed"
    recipe_yaml.write_text(yaml.safe_dump(payload), encoding="utf-8")

    recipes_dir = tmp_path / "recipes"
    slug, destination = copy_recipe_into_recipes_dir(source_recipe, recipes_dir)

    assert slug == "renamed"
    assert destination == recipes_dir / "renamed"
    assert (destination / "recipe.yaml").exists()


def test_discovery_entry_includes_manifest_record(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir / "alpha")
    record_plugin_install(
        recipes_dir,
        slug="alpha",
        folder="alpha",
        source_type="git",
        source="https://example.com/repo.git",
        source_ref="v1.0.0",
        trusted=False,
    )

    entries = discover_plugin_entries(recipes_dir)
    alpha = find_plugin_entry(entries, "alpha")
    assert alpha is not None
    assert alpha.manifest_record is not None
    assert alpha.manifest_record["source_type"] == "git"
    assert alpha.manifest_record["trusted"] is False


def test_load_catalog_reads_plugin_entries(tmp_path: Path) -> None:
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "demo": {
                        "source": "./demo",
                        "trusted": True,
                        "description": "demo plugin",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(catalog_file)
    assert "demo" in catalog
    assert catalog["demo"]["source"] == "./demo"
    assert catalog["demo"]["trusted"] is True


def test_default_paths_exist() -> None:
    assert default_recipes_dir().exists()
    assert DEFAULT_CATALOG_PATH.exists()
