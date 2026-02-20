"""Unit tests for Web2API CLI command behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from web2api import cli
from web2api.plugin import parse_plugin_config
from web2api.recipe_manager import CatalogRecipeSpec, RecipeEntry


def test_recipes_install_defaults_to_no_apt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin = parse_plugin_config({"version": "1.0.0"})
    entry = RecipeEntry(
        slug="x",
        folder="x",
        path=tmp_path,
        enabled=True,
        has_recipe=True,
        plugin=plugin,
        error=None,
    )

    captured: dict[str, bool] = {}

    def _fake_build_install_commands(
        plugin_config, *, include_apt: bool, include_npm: bool, include_python: bool
    ) -> list[list[str]]:
        _ = plugin_config
        captured["include_apt"] = include_apt
        captured["include_npm"] = include_npm
        captured["include_python"] = include_python
        return []

    monkeypatch.setattr("web2api.cli.discover_recipe_entries", lambda recipes_dir: [entry])
    monkeypatch.setattr("web2api.cli.build_install_commands", _fake_build_install_commands)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["recipes", "install", "x", "--yes", "--dry-run"])

    assert result.exit_code == 0

    assert captured["include_apt"] is False
    assert captured["include_npm"] is True
    assert captured["include_python"] is True


def test_recipes_list_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("web2api.cli.discover_recipe_entries", lambda recipes_dir: [])

    runner = CliRunner()
    result = runner.invoke(cli.app, ["recipes", "list"])

    assert result.exit_code == 0
    assert "No recipe folders found" in result.output


def test_self_update_apply_runs_recipes_doctor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = {"doctor": False, "apply": False}

    monkeypatch.setattr("web2api.cli.detect_update_method", lambda workdir: "pip")
    monkeypatch.setattr(
        "web2api.cli.build_update_commands",
        lambda method, to_version=None: [["echo", "updated"]],
    )

    def _fake_apply_update_commands(commands: list[list[str]], *, dry_run: bool) -> None:
        _ = commands, dry_run
        called["apply"] = True

    def _fake_doctor(
        *,
        slug: str | None,
        recipes_dir: Path | None,
        json_output: bool,
        run_healthchecks: bool,
        allow_untrusted: bool,
        healthcheck_timeout: float,
    ) -> None:
        _ = slug, recipes_dir, json_output, run_healthchecks, allow_untrusted, healthcheck_timeout
        called["doctor"] = True

    monkeypatch.setattr("web2api.cli.apply_update_commands", _fake_apply_update_commands)
    monkeypatch.setattr("web2api.cli.recipes_doctor", _fake_doctor)

    cli.self_update_apply(
        method="auto",
        to=None,
        workdir=tmp_path,
        yes=True,
        dry_run=True,
    )

    assert called["apply"] is True
    assert called["doctor"] is True


def test_self_update_apply_does_not_fail_on_doctor_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("web2api.cli.detect_update_method", lambda workdir: "pip")
    monkeypatch.setattr(
        "web2api.cli.build_update_commands",
        lambda method, to_version=None: [["echo", "updated"]],
    )
    monkeypatch.setattr(
        "web2api.cli.apply_update_commands",
        lambda commands, dry_run: None,
    )
    monkeypatch.setattr(
        "web2api.cli.recipes_doctor",
        lambda **kwargs: (_ for _ in ()).throw(typer.Exit(code=1)),
    )

    cli.self_update_apply(
        method="auto",
        to=None,
        workdir=tmp_path,
        yes=True,
        dry_run=True,
    )


def test_recipes_uninstall_requires_force_for_unmanaged(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = RecipeEntry(
        slug="x",
        folder="x",
        path=Path("/tmp/x"),
        enabled=True,
        has_recipe=True,
        plugin=None,
        error=None,
        manifest_record=None,
    )
    monkeypatch.setattr("web2api.cli.discover_recipe_entries", lambda recipes_dir: [entry])
    monkeypatch.setattr(
        "web2api.cli.load_manifest",
        lambda recipes_dir: {"version": 1, "recipes": {}},
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["recipes", "uninstall", "x", "--yes"])

    assert result.exit_code == 1
    assert "not tracked in manifest" in result.output


def test_recipes_update_uses_manifest_source_and_preserves_disabled_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = RecipeEntry(
        slug="x",
        folder="x",
        path=tmp_path / "x",
        enabled=False,
        has_recipe=True,
        plugin=None,
        error=None,
        manifest_record={
            "source_type": "catalog",
            "source": "https://example.com/repo.git",
            "source_ref": "v1.0.0",
            "source_subdir": "recipes/x",
            "trusted": False,
        },
    )
    monkeypatch.setattr("web2api.cli.discover_recipe_entries", lambda recipes_dir: [entry])
    monkeypatch.setattr(
        "web2api.cli.load_manifest",
        lambda recipes_dir: {
            "version": 1,
            "recipes": {
                "x": {
                    "source_type": "catalog",
                    "source": "https://example.com/repo.git",
                    "source_ref": "v1.0.0",
                    "source_subdir": "recipes/x",
                    "trusted": False,
                }
            },
        },
    )
    monkeypatch.setattr("web2api.cli._confirm_or_exit", lambda prompt, yes: None)

    captured: dict[str, object] = {}

    def _fake_add_recipe_from_source(**kwargs):
        captured.update(kwargs)
        return "x", "catalog"

    disabled: dict[str, Path] = {}

    def _fake_disable_recipe(path: Path) -> None:
        disabled["path"] = path

    monkeypatch.setattr("web2api.cli._add_recipe_from_source", _fake_add_recipe_from_source)
    monkeypatch.setattr("web2api.cli.disable_recipe", _fake_disable_recipe)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "recipes",
            "update",
            "x",
            "--recipes-dir",
            str(tmp_path),
            "--yes",
            "--ref",
            "v2.0.0",
            "--subdir",
            "updated/subdir",
        ],
    )

    assert result.exit_code == 0
    assert captured["source"] == "https://example.com/repo.git"
    assert captured["source_ref"] == "v2.0.0"
    assert captured["source_subdir"] == "updated/subdir"
    assert captured["record_source_type"] == "catalog"
    assert captured["expected_slug"] == "x"
    assert captured["overwrite"] is True
    assert captured["yes"] is True
    assert disabled["path"] == tmp_path / "x"


def test_recipes_update_requires_managed_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = RecipeEntry(
        slug="x",
        folder="x",
        path=Path("/tmp/x"),
        enabled=True,
        has_recipe=True,
        plugin=None,
        error=None,
        manifest_record=None,
    )
    monkeypatch.setattr("web2api.cli.discover_recipe_entries", lambda recipes_dir: [entry])
    monkeypatch.setattr(
        "web2api.cli.load_manifest",
        lambda recipes_dir: {"version": 1, "recipes": {}},
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["recipes", "update", "x", "--yes"])

    assert result.exit_code == 1
    assert "not tracked in manifest" in result.output


def test_recipes_uninstall_removes_manifest_record(monkeypatch: pytest.MonkeyPatch) -> None:
    removed = {"called": False}
    recipe_path = Path("/tmp/x")
    entry = RecipeEntry(
        slug="x",
        folder="x",
        path=recipe_path,
        enabled=True,
        has_recipe=True,
        plugin=None,
        error=None,
        manifest_record={"trusted": True},
    )

    monkeypatch.setattr("web2api.cli.discover_recipe_entries", lambda recipes_dir: [entry])
    monkeypatch.setattr(
        "web2api.cli.load_manifest",
        lambda recipes_dir: {"version": 1, "recipes": {"x": {"folder": "x"}}},
    )
    monkeypatch.setattr("web2api.cli.get_manifest_record", lambda manifest, slug: {"folder": "x"})
    monkeypatch.setattr("web2api.cli._confirm_or_exit", lambda prompt, yes: None)

    def _fake_remove_manifest_record(recipes_dir, slug):
        removed["called"] = True
        return True

    monkeypatch.setattr("web2api.cli.remove_manifest_record", _fake_remove_manifest_record)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["recipes", "uninstall", "x", "--yes", "--keep-files"])

    assert result.exit_code == 0
    assert removed["called"] is True


def test_recipes_catalog_add_installs_from_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: dict[str, object] = {}

    monkeypatch.setattr(
        "web2api.cli.resolve_catalog_recipes",
        lambda **kwargs: {
            "demo": CatalogRecipeSpec(
                name="demo",
                slug="demo",
                source="./demo-plugin",
                source_ref="v1.0.0",
                source_subdir=None,
                description=None,
                trusted=True,
            )
        },
    )

    def _fake_add_recipe_from_source(**kwargs):
        called.update(kwargs)
        return "demo", "catalog"

    monkeypatch.setattr("web2api.cli._add_recipe_from_source", _fake_add_recipe_from_source)

    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text("recipes: {}\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["recipes", "catalog", "add", "demo", "--catalog-source", str(catalog_file), "--yes"],
    )

    assert result.exit_code == 0
    assert called["record_source_type"] == "catalog"
