"""Unit tests for recipe manager helpers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from web2api.plugin import parse_plugin_config
from web2api.recipe_manager import (
    OFFICIAL_RECIPES_REPO_URL,
    build_install_commands,
    copy_recipe_into_recipes_dir,
    default_catalog_source,
    default_recipes_dir,
    disable_recipe,
    discover_recipe_entries,
    enable_recipe,
    find_recipe_entry,
    install_recipe_from_source,
    load_catalog,
    load_manifest,
    record_recipe_install,
    remove_manifest_record,
    resolve_catalog_recipes,
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


def test_discover_recipe_entries_includes_enablement_and_plugin(tmp_path: Path) -> None:
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

    entries = discover_recipe_entries(recipes_dir)
    alpha = find_recipe_entry(entries, "alpha")
    assert alpha is not None
    assert alpha.enabled is False
    assert alpha.plugin is not None
    assert alpha.plugin.requires_env == ["ALPHA_TOKEN"]

    enable_recipe(alpha_dir)
    entries_after_enable = discover_recipe_entries(recipes_dir)
    alpha_enabled = find_recipe_entry(entries_after_enable, "alpha")
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

    record_recipe_install(
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
    assert manifest["recipes"]["alpha"]["source_type"] == "local"
    assert manifest["recipes"]["alpha"]["source_subdir"] == "recipes/alpha"
    assert manifest["recipes"]["alpha"]["trusted"] is True

    removed = remove_manifest_record(recipes_dir, "alpha")
    assert removed is True
    assert load_manifest(recipes_dir)["recipes"] == {}


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
    record_recipe_install(
        recipes_dir,
        slug="alpha",
        folder="alpha",
        source_type="git",
        source="https://example.com/repo.git",
        source_ref="v1.0.0",
        trusted=False,
    )

    entries = discover_recipe_entries(recipes_dir)
    alpha = find_recipe_entry(entries, "alpha")
    assert alpha is not None
    assert alpha.manifest_record is not None
    assert alpha.manifest_record["source_type"] == "git"
    assert alpha.manifest_record["trusted"] is False


def test_load_catalog_reads_recipe_entries(tmp_path: Path) -> None:
    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        yaml.safe_dump(
            {
                "recipes": {
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


def test_default_paths_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB2API_RECIPE_CATALOG_SOURCE", raising=False)
    recipes_path = default_recipes_dir()
    assert recipes_path.name == "recipes"
    assert recipes_path.parent.name == ".web2api"
    assert default_catalog_source() == OFFICIAL_RECIPES_REPO_URL


def test_resolve_catalog_recipes_from_local_file(tmp_path: Path) -> None:
    source_recipe = tmp_path / "demo-recipe"
    _write_recipe(source_recipe)

    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(
        yaml.safe_dump(
            {
                "recipes": {
                    "demo": {
                        "source": "./demo-recipe",
                        "trusted": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    specs = resolve_catalog_recipes(catalog_source=str(catalog_file))
    assert "demo" in specs
    assert specs["demo"].slug == "demo"
    assert specs["demo"].source == str(source_recipe.resolve())
    assert specs["demo"].trusted is True


def test_resolve_catalog_recipes_sparse_checkout_for_remote_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    repo_path: Path | None = None

    def _fake_run(command, check: bool, text: bool, **kwargs):  # noqa: ANN001
        del check, text, kwargs
        nonlocal repo_path
        command_list = [str(part) for part in command]
        commands.append(command_list)
        if command_list[:3] == ["git", "init", "--quiet"]:
            repo_path = Path(command_list[3])
            repo_path.mkdir(parents=True, exist_ok=True)
        elif repo_path is not None and command_list[:3] == ["git", "-C", str(repo_path)]:
            if command_list[3:6] == ["checkout", "--quiet", "FETCH_HEAD"]:
                (repo_path / "catalog.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "recipes": {
                                "demo": {
                                    "source": "recipes/demo",
                                    "description": "demo",
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(command_list, 0)

    monkeypatch.setattr("web2api.recipe_manager.subprocess.run", _fake_run)

    specs = resolve_catalog_recipes(catalog_source="https://example.com/catalog.git")

    assert "demo" in specs
    assert specs["demo"].source == "https://example.com/catalog.git"
    assert specs["demo"].source_subdir == "recipes/demo"

    assert any(
        command[:9]
        == [
            "git",
            "-C",
            str(repo_path),
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
        ]
        for command in commands
        if repo_path is not None
    )
    assert any(
        command
        == [
            "git",
            "-C",
            str(repo_path),
            "sparse-checkout",
            "set",
            "catalog.yaml",
        ]
        for command in commands
        if repo_path is not None
    )


def test_install_recipe_from_source_sparse_checkout_for_subdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    repo_path: Path | None = None

    def _fake_run(command, check: bool, text: bool, **kwargs):  # noqa: ANN001
        del check, text, kwargs
        nonlocal repo_path
        command_list = [str(part) for part in command]
        commands.append(command_list)
        if command_list[:3] == ["git", "init", "--quiet"]:
            repo_path = Path(command_list[3])
            repo_path.mkdir(parents=True, exist_ok=True)
        elif repo_path is not None and command_list[:3] == ["git", "-C", str(repo_path)]:
            if command_list[3:6] == ["checkout", "--quiet", "FETCH_HEAD"]:
                source_recipe_dir = repo_path / "recipes" / "demo"
                _write_recipe(source_recipe_dir)
        return subprocess.CompletedProcess(command_list, 0)

    monkeypatch.setattr("web2api.recipe_manager.subprocess.run", _fake_run)

    slug, source_type = install_recipe_from_source(
        source="https://example.com/recipes.git",
        recipes_dir=tmp_path / "recipes",
        source_ref=None,
        source_subdir="recipes/demo",
        trusted=True,
    )

    assert slug == "demo"
    assert source_type == "git"
    assert (tmp_path / "recipes" / "demo" / "recipe.yaml").exists()
    assert any(
        command
        == [
            "git",
            "-C",
            str(repo_path),
            "sparse-checkout",
            "set",
            "recipes/demo",
        ]
        for command in commands
        if repo_path is not None
    )
