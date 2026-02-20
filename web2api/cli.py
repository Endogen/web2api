"""Command line interface for Web2API plugin and self-update workflows."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

import typer

from web2api import __version__
from web2api.plugin_manager import (
    DEFAULT_CATALOG_PATH,
    SourceType,
    build_dockerfile_snippet,
    build_install_commands,
    checkout_source,
    copy_recipe_into_recipes_dir,
    disable_recipe,
    discover_plugin_entries,
    enable_recipe,
    find_plugin_entry,
    get_manifest_record,
    load_catalog,
    load_manifest,
    load_source_recipe_slug,
    plugin_status_payload,
    record_plugin_install,
    remove_manifest_record,
    resolve_recipe_source_dir,
    resolve_recipes_dir,
    resolve_source_type,
    run_commands,
    run_healthcheck,
)
from web2api.self_update import (
    UpdateMethod,
    apply_update_commands,
    build_update_commands,
    check_for_updates,
    detect_update_method,
    resolve_latest_git_tag,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Web2API management CLI.",
    add_completion=False,
)
plugins_app = typer.Typer(no_args_is_help=True, help="Manage recipe plugins.")
catalog_app = typer.Typer(no_args_is_help=True, help="Plugin source catalog commands.")
self_app = typer.Typer(no_args_is_help=True, help="Manage Web2API installation.")
update_app = typer.Typer(no_args_is_help=True, help="Self-update commands.")

app.add_typer(plugins_app, name="plugins")
plugins_app.add_typer(catalog_app, name="catalog")
app.add_typer(self_app, name="self")
self_app.add_typer(update_app, name="update")


def _recipes_dir_option(recipes_dir: Path | None) -> Path:
    return resolve_recipes_dir(recipes_dir)


def _print_command(command: list[str]) -> None:
    typer.echo(f"$ {shlex.join(command)}")


def _confirm_or_exit(prompt: str, *, yes: bool) -> None:
    if yes:
        return
    if typer.confirm(prompt):
        return
    raise typer.Exit(code=1)


def _entry_trusted(entry_record: dict[str, Any] | None) -> bool:
    if isinstance(entry_record, dict) and isinstance(entry_record.get("trusted"), bool):
        return bool(entry_record["trusted"])
    return True


def _resolve_catalog_path(catalog_file: Path | None) -> Path:
    return DEFAULT_CATALOG_PATH if catalog_file is None else catalog_file


def _resolve_catalog_source(raw_source: str, catalog_path: Path) -> str:
    candidate = Path(raw_source).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    if "://" in raw_source or raw_source.startswith("git@"):
        return raw_source
    return str((catalog_path.parent / candidate).resolve())


def _add_plugin_from_source(
    *,
    source: str,
    source_ref: str | None,
    source_subdir: str | None,
    recipes_dir: Path,
    trusted: bool,
    overwrite: bool,
    yes: bool,
    record_source_type: SourceType | None = None,
    expected_slug: str | None = None,
) -> tuple[str, SourceType]:
    resolved_source_type = resolve_source_type(source)
    manifest_source_type = record_source_type or resolved_source_type

    if resolved_source_type == "git":
        _confirm_or_exit(
            f"Fetch plugin source from git repository '{source}'?",
            yes=yes,
        )

    with checkout_source(
        source,
        source_ref=source_ref,
        source_type=resolved_source_type,
    ) as source_root:
        source_recipe_dir = resolve_recipe_source_dir(source_root, source_subdir)
        source_slug = load_source_recipe_slug(source_recipe_dir)
        if expected_slug is not None and source_slug != expected_slug:
            raise ValueError(
                f"source recipe slug '{source_slug}' does not match expected slug '{expected_slug}'"
            )
        slug, destination = copy_recipe_into_recipes_dir(
            source_recipe_dir,
            recipes_dir,
            overwrite=overwrite,
        )

    record_plugin_install(
        recipes_dir,
        slug=slug,
        folder=destination.name,
        source_type=manifest_source_type,
        source=source,
        source_ref=source_ref,
        source_subdir=source_subdir,
        trusted=trusted,
    )
    return slug, manifest_source_type


@plugins_app.command("list")
def plugins_list(
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List recipes, plugin availability, and install-state metadata."""
    target_dir = _recipes_dir_option(recipes_dir)
    entries = discover_plugin_entries(target_dir)

    payload: list[dict[str, object]] = []
    for entry in entries:
        plugin_payload = None
        if entry.plugin is not None:
            plugin_payload = plugin_status_payload(entry.plugin, app_version=__version__)

        trusted = _entry_trusted(entry.manifest_record)
        source_type = None
        source = None
        managed = False
        if entry.manifest_record is not None:
            managed = True
            source_type_raw = entry.manifest_record.get("source_type")
            source_raw = entry.manifest_record.get("source")
            source_type = str(source_type_raw) if source_type_raw is not None else None
            source = str(source_raw) if source_raw is not None else None

        payload.append(
            {
                "slug": entry.slug,
                "folder": entry.folder,
                "enabled": entry.enabled,
                "has_recipe": entry.has_recipe,
                "managed": managed,
                "trusted": trusted,
                "source_type": source_type,
                "source": source,
                "error": entry.error,
                "plugin": plugin_payload,
                "path": str(entry.path),
            }
        )

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not payload:
        typer.echo(f"No recipe folders found in {target_dir}.")
        return

    for item in payload:
        plugin_block = item["plugin"]
        plugin_ready = "-"
        plugin_version = "-"
        if isinstance(plugin_block, dict):
            status = plugin_block.get("status", {})
            if isinstance(status, dict):
                plugin_ready = str(status.get("ready"))
            plugin_version = str(plugin_block.get("version"))

        state = "enabled" if item["enabled"] else "disabled"
        managed = "managed" if item["managed"] else "unmanaged"
        trusted = "trusted" if item["trusted"] else "untrusted"
        source_label = str(item["source_type"] or "-")
        line = (
            f"{item['slug']}: state={state}, {managed}, {trusted}, source={source_label}, "
            f"plugin={plugin_version}, ready={plugin_ready}, path={item['path']}"
        )
        typer.echo(line)
        if item["error"]:
            typer.echo(f"  error: {item['error']}")


@plugins_app.command("doctor")
def plugins_doctor(
    slug: str | None = typer.Argument(default=None, help="Recipe slug (optional)."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
    run_healthchecks: bool = typer.Option(
        True,
        "--run-healthchecks/--no-run-healthchecks",
        help="Run plugin healthcheck commands when configured.",
    ),
    allow_untrusted: bool = typer.Option(
        False,
        "--allow-untrusted",
        help="Allow executing checks for plugins marked as untrusted.",
    ),
    healthcheck_timeout: float = typer.Option(
        15.0,
        "--healthcheck-timeout",
        min=1.0,
        help="Timeout in seconds for each plugin healthcheck command.",
    ),
) -> None:
    """Show plugin readiness details and optional healthcheck results."""
    entries = discover_plugin_entries(_recipes_dir_option(recipes_dir))

    if slug is not None:
        selected = find_plugin_entry(entries, slug)
        entries = [selected] if selected is not None else []
    if not entries:
        typer.echo("No matching recipes found.", err=True)
        raise typer.Exit(code=1)

    report: list[dict[str, object]] = []
    for entry in entries:
        trusted = _entry_trusted(entry.manifest_record)
        status_payload = None
        if entry.plugin is not None:
            status_payload = plugin_status_payload(entry.plugin, app_version=__version__)

        healthcheck_payload: dict[str, Any] | None = None
        if entry.plugin is not None and run_healthchecks:
            if not trusted and not allow_untrusted:
                if entry.plugin.healthcheck is not None:
                    healthcheck_payload = {
                        "defined": True,
                        "ran": False,
                        "ok": None,
                        "skipped": "untrusted plugin; pass --allow-untrusted to run healthcheck",
                    }
            else:
                healthcheck_payload = run_healthcheck(
                    entry.plugin,
                    timeout_seconds=healthcheck_timeout,
                )

        report.append(
            {
                "slug": entry.slug,
                "enabled": entry.enabled,
                "trusted": trusted,
                "plugin": status_payload,
                "healthcheck": healthcheck_payload,
                "error": entry.error,
            }
        )

    if json_output:
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report:
            slug_value = str(item["slug"])
            trusted = "trusted" if item["trusted"] else "untrusted"
            if not item["enabled"]:
                typer.echo(f"{slug_value}: disabled ({trusted})")
                continue
            plugin_block = item["plugin"]
            if not isinstance(plugin_block, dict):
                typer.echo(f"{slug_value}: no plugin.yaml ({trusted})")
                continue
            status = plugin_block.get("status")
            if not isinstance(status, dict):
                typer.echo(f"{slug_value}: plugin status unavailable ({trusted})")
                continue
            ready = status.get("ready")
            typer.echo(f"{slug_value}: ready={ready} ({trusted})")
            checks = status.get("checks")
            if isinstance(checks, dict):
                for name in ("env", "commands", "python"):
                    detail = checks.get(name)
                    if isinstance(detail, dict):
                        missing = detail.get("missing", [])
                        if missing:
                            typer.echo(f"  missing {name}: {', '.join(str(v) for v in missing)}")

            health = item["healthcheck"]
            if isinstance(health, dict) and health.get("defined"):
                if health.get("skipped"):
                    typer.echo(f"  healthcheck: skipped ({health['skipped']})")
                elif health.get("ok") is True:
                    typer.echo("  healthcheck: ok")
                elif health.get("ok") is False:
                    exit_code = health.get("exit_code")
                    typer.echo(f"  healthcheck: failed (exit_code={exit_code})")
                    stderr = str(health.get("stderr") or "").strip()
                    if stderr:
                        typer.echo(f"  healthcheck stderr: {stderr.splitlines()[0]}")

    failed = False
    for item in report:
        plugin_block = item["plugin"]
        if isinstance(plugin_block, dict):
            status = plugin_block.get("status")
            if isinstance(status, dict) and status.get("ready") is False:
                failed = True

        health = item.get("healthcheck")
        if isinstance(health, dict) and health.get("defined") and health.get("ok") is False:
            failed = True
    if failed:
        raise typer.Exit(code=1)


@plugins_app.command("install")
def plugins_install(
    slug: str = typer.Argument(help="Recipe slug to install dependencies for."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Run install commands without prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print commands without executing."),
    include_apt: bool = typer.Option(
        False,
        "--apt/--no-apt",
        help="Install apt dependencies (disabled by default).",
    ),
    include_npm: bool = typer.Option(True, "--npm/--no-npm", help="Install npm dependencies."),
    include_python: bool = typer.Option(
        True,
        "--python/--no-python",
        help="Install Python package dependencies.",
    ),
    allow_untrusted: bool = typer.Option(
        False,
        "--allow-untrusted",
        help="Allow command execution for plugins marked as untrusted.",
    ),
    target: Literal["host", "docker"] = typer.Option(
        "host",
        "--target",
        help="Install target: host executes commands, docker prints Dockerfile RUN snippet.",
    ),
) -> None:
    """Install dependencies declared in plugin.yaml."""
    entries = discover_plugin_entries(_recipes_dir_option(recipes_dir))
    entry = find_plugin_entry(entries, slug)
    if entry is None:
        typer.echo(f"Recipe '{slug}' was not found.", err=True)
        raise typer.Exit(code=1)
    if entry.plugin is None:
        typer.echo(f"Recipe '{entry.slug}' has no plugin.yaml.", err=True)
        raise typer.Exit(code=1)

    trusted = _entry_trusted(entry.manifest_record)
    if not trusted and not allow_untrusted:
        typer.echo(
            f"Plugin '{entry.slug}' is marked untrusted. "
            "Pass --allow-untrusted to execute install commands.",
            err=True,
        )
        raise typer.Exit(code=1)

    commands = build_install_commands(
        entry.plugin,
        include_apt=include_apt,
        include_npm=include_npm,
        include_python=include_python,
    )
    if target == "docker":
        typer.echo(build_dockerfile_snippet(commands))
    elif not commands:
        typer.echo("No install actions are defined for this plugin.")
    else:
        for command in commands:
            _print_command(command)
        _confirm_or_exit(
            f"Run {len(commands)} install command(s) for '{entry.slug}'?",
            yes=yes or dry_run,
        )
        try:
            run_commands(commands, dry_run=dry_run)
        except FileNotFoundError as exc:
            typer.echo(f"Required executable not found: {exc.filename}", err=True)
            raise typer.Exit(code=1) from exc
        except subprocess.CalledProcessError as exc:
            typer.echo(f"Install command failed with exit code {exc.returncode}.", err=True)
            raise typer.Exit(code=exc.returncode) from exc

    payload = plugin_status_payload(entry.plugin, app_version=__version__)
    status = payload["status"]
    typer.echo(f"Plugin '{entry.slug}' ready={status['ready']}")
    checks = status["checks"]
    for name in ("env", "commands", "python"):
        missing = checks[name]["missing"]
        if missing:
            typer.echo(f"  missing {name}: {', '.join(missing)}")


@plugins_app.command("add")
def plugins_add(
    source: str = typer.Argument(help="Plugin source path or git URL."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to install into (defaults to RECIPES_DIR or ./recipes).",
    ),
    source_ref: str | None = typer.Option(None, "--ref", help="Git branch/tag/commit to checkout."),
    source_subdir: str | None = typer.Option(
        None,
        "--subdir",
        help="Subdirectory inside source that contains recipe.yaml.",
    ),
    trusted: bool = typer.Option(
        False,
        "--trusted",
        help="Mark source as trusted (required for untrusted command execution defaults).",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing recipe folder."),
    yes: bool = typer.Option(False, "--yes", help="Continue without interactive confirmation."),
) -> None:
    """Install a plugin recipe from local path or git source."""
    target_dir = _recipes_dir_option(recipes_dir)
    source_type = resolve_source_type(source)
    trusted_value = trusted or source_type == "local"

    try:
        slug, recorded_type = _add_plugin_from_source(
            source=source,
            source_ref=source_ref,
            source_subdir=source_subdir,
            recipes_dir=target_dir,
            trusted=trusted_value,
            overwrite=overwrite,
            yes=yes,
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to install plugin from source: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    trust_label = "trusted" if trusted_value else "untrusted"
    typer.echo(
        f"Installed plugin '{slug}' from {recorded_type} source ({trust_label}). "
        f"Run `web2api plugins install {slug}` to install dependencies."
    )


@plugins_app.command("update")
def plugins_update(
    slug: str = typer.Argument(help="Recipe slug to update from recorded source."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
    source_ref: str | None = typer.Option(
        None,
        "--ref",
        help="Override recorded source ref for this update.",
    ),
    source_subdir: str | None = typer.Option(
        None,
        "--subdir",
        help="Override recorded source subdirectory for this update.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Update without prompt."),
) -> None:
    """Update a managed plugin from its recorded source."""
    target_dir = _recipes_dir_option(recipes_dir)
    entries = discover_plugin_entries(target_dir)
    entry = find_plugin_entry(entries, slug)
    manifest = load_manifest(target_dir)
    manifest_record = get_manifest_record(manifest, slug)

    if manifest_record is None:
        typer.echo(
            f"Plugin '{slug}' is not tracked in manifest. "
            "Use `web2api plugins add` to install managed plugins.",
            err=True,
        )
        raise typer.Exit(code=1)

    source_raw = manifest_record.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        typer.echo(
            f"Plugin '{slug}' has no recorded source in manifest.",
            err=True,
        )
        raise typer.Exit(code=1)
    source = source_raw.strip()

    manifest_source_ref = manifest_record.get("source_ref")
    source_ref_value = source_ref if source_ref is not None else None
    if source_ref_value is None and isinstance(manifest_source_ref, str):
        source_ref_value = manifest_source_ref

    manifest_source_subdir = manifest_record.get("source_subdir")
    source_subdir_value = source_subdir if source_subdir is not None else None
    if source_subdir_value is None and isinstance(manifest_source_subdir, str):
        source_subdir_value = manifest_source_subdir

    trusted = _entry_trusted(manifest_record)
    manifest_source_type_raw = manifest_record.get("source_type")
    record_source_type: SourceType | None = None
    if manifest_source_type_raw == "local":
        record_source_type = "local"
    elif manifest_source_type_raw == "git":
        record_source_type = "git"
    elif manifest_source_type_raw == "catalog":
        record_source_type = "catalog"

    was_disabled = entry is not None and not entry.enabled
    _confirm_or_exit(
        f"Update plugin '{slug}' from source '{source}'?",
        yes=yes,
    )

    try:
        updated_slug, recorded_type = _add_plugin_from_source(
            source=source,
            source_ref=source_ref_value,
            source_subdir=source_subdir_value,
            recipes_dir=target_dir,
            trusted=trusted,
            overwrite=True,
            yes=True,
            record_source_type=record_source_type,
            expected_slug=slug,
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to update plugin '{slug}': {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if was_disabled:
        disable_recipe(target_dir / updated_slug)

    trust_label = "trusted" if trusted else "untrusted"
    typer.echo(f"Updated plugin '{updated_slug}' from {recorded_type} source ({trust_label}).")


@plugins_app.command("uninstall")
def plugins_uninstall(
    slug: str = typer.Argument(help="Recipe slug to uninstall."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Uninstall without prompt."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow uninstalling plugins not tracked in manifest.",
    ),
    delete_files: bool = typer.Option(
        True,
        "--delete-files/--keep-files",
        help="Delete recipe directory on uninstall.",
    ),
) -> None:
    """Uninstall a plugin recipe and remove its manifest record."""
    target_dir = _recipes_dir_option(recipes_dir)
    entries = discover_plugin_entries(target_dir)
    entry = find_plugin_entry(entries, slug)
    manifest = load_manifest(target_dir)
    manifest_record = get_manifest_record(manifest, slug)

    if entry is None and manifest_record is None:
        typer.echo(f"Plugin '{slug}' was not found in recipes or manifest.", err=True)
        raise typer.Exit(code=1)

    if manifest_record is None and not force:
        typer.echo(
            f"Plugin '{slug}' is not tracked in manifest. "
            "Use `plugins disable` or pass --force to remove files anyway.",
            err=True,
        )
        raise typer.Exit(code=1)

    folder = None
    if entry is not None:
        folder = entry.folder
    elif isinstance(manifest_record, dict):
        folder = str(manifest_record.get("folder") or slug)
    else:
        folder = slug

    recipe_path = target_dir / folder
    _confirm_or_exit(
        f"Uninstall plugin '{slug}' (folder: {recipe_path})?",
        yes=yes,
    )

    if delete_files and recipe_path.exists():
        shutil.rmtree(recipe_path)
        typer.echo(f"Deleted recipe directory: {recipe_path}")

    removed = remove_manifest_record(target_dir, slug)
    if removed:
        typer.echo(f"Removed manifest record for '{slug}'.")

    if not removed and not recipe_path.exists():
        typer.echo(f"Plugin '{slug}' was not installed or already removed.")
    else:
        typer.echo(f"Uninstalled plugin '{slug}'.")


@plugins_app.command("enable")
def plugins_enable(
    slug: str = typer.Argument(help="Recipe slug to enable."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
) -> None:
    """Enable a recipe by removing .disabled marker."""
    entries = discover_plugin_entries(_recipes_dir_option(recipes_dir))
    entry = find_plugin_entry(entries, slug)
    if entry is None:
        typer.echo(f"Recipe '{slug}' was not found.", err=True)
        raise typer.Exit(code=1)
    if entry.enabled:
        typer.echo(f"Recipe '{entry.slug}' is already enabled.")
        return
    enable_recipe(entry.path)
    typer.echo(f"Enabled recipe '{entry.slug}'.")


@plugins_app.command("disable")
def plugins_disable(
    slug: str = typer.Argument(help="Recipe slug to disable."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ./recipes).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Disable without prompt."),
) -> None:
    """Disable a recipe by writing .disabled marker."""
    entries = discover_plugin_entries(_recipes_dir_option(recipes_dir))
    entry = find_plugin_entry(entries, slug)
    if entry is None:
        typer.echo(f"Recipe '{slug}' was not found.", err=True)
        raise typer.Exit(code=1)
    if not entry.enabled:
        typer.echo(f"Recipe '{entry.slug}' is already disabled.")
        return
    _confirm_or_exit(f"Disable recipe '{entry.slug}'?", yes=yes)
    disable_recipe(entry.path)
    typer.echo(f"Disabled recipe '{entry.slug}'.")


@catalog_app.command("list")
def plugins_catalog_list(
    catalog_file: Path | None = typer.Option(
        None,
        "--catalog-file",
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="Path to plugin catalog YAML (defaults to plugins/catalog.yaml).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List available plugin catalog entries."""
    resolved_catalog = _resolve_catalog_path(catalog_file)
    try:
        catalog = load_catalog(resolved_catalog)
    except ValueError as exc:
        typer.echo(f"Invalid catalog: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(catalog, indent=2, sort_keys=True))
        return

    if not catalog:
        typer.echo(f"No catalog entries found in {resolved_catalog}.")
        return

    typer.echo(f"Catalog: {resolved_catalog}")
    for name, entry in sorted(catalog.items()):
        source = str(entry.get("source", ""))
        description = str(entry.get("description") or "")
        trusted = entry.get("trusted")
        trusted_label = "trusted" if trusted is True else "untrusted"
        typer.echo(f"{name}: source={source}, {trusted_label}")
        if description:
            typer.echo(f"  {description}")


@catalog_app.command("add")
def plugins_catalog_add(
    name: str = typer.Argument(help="Catalog plugin entry name to install."),
    catalog_file: Path | None = typer.Option(
        None,
        "--catalog-file",
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help="Path to plugin catalog YAML (defaults to plugins/catalog.yaml).",
    ),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to install into (defaults to RECIPES_DIR or ./recipes).",
    ),
    trusted: bool = typer.Option(
        False,
        "--trusted",
        help="Override catalog trust and mark source as trusted.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing recipe folder."),
    yes: bool = typer.Option(False, "--yes", help="Continue without interactive confirmation."),
) -> None:
    """Install a plugin recipe from configured catalog entry."""
    resolved_catalog = _resolve_catalog_path(catalog_file)
    try:
        catalog = load_catalog(resolved_catalog)
    except ValueError as exc:
        typer.echo(f"Invalid catalog: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    entry = catalog.get(name)
    if entry is None:
        typer.echo(f"Catalog entry '{name}' was not found in {resolved_catalog}.", err=True)
        raise typer.Exit(code=1)

    raw_source = str(entry.get("source") or "")
    if not raw_source:
        typer.echo(f"Catalog entry '{name}' has empty source.", err=True)
        raise typer.Exit(code=1)

    source = _resolve_catalog_source(raw_source, resolved_catalog)
    source_ref_raw = entry.get("ref")
    source_ref = str(source_ref_raw) if isinstance(source_ref_raw, str) else None
    source_subdir_raw = entry.get("subdir")
    source_subdir = str(source_subdir_raw) if isinstance(source_subdir_raw, str) else None

    catalog_trusted = entry.get("trusted")
    trusted_value = trusted or bool(catalog_trusted)

    target_dir = _recipes_dir_option(recipes_dir)

    try:
        slug, _ = _add_plugin_from_source(
            source=source,
            source_ref=source_ref,
            source_subdir=source_subdir,
            recipes_dir=target_dir,
            trusted=trusted_value,
            overwrite=overwrite,
            yes=yes,
            record_source_type="catalog",
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to install catalog plugin '{name}': {exc}", err=True)
        raise typer.Exit(code=1) from exc

    trust_label = "trusted" if trusted_value else "untrusted"
    typer.echo(f"Installed catalog plugin '{name}' as slug '{slug}' ({trust_label}).")


@self_app.command("version")
def self_version() -> None:
    """Print installed Web2API version."""
    typer.echo(__version__)


@update_app.command("check")
def self_update_check(
    method: UpdateMethod = typer.Option(
        "auto",
        "--method",
        help="Update method to evaluate (auto, pip, git, docker).",
    ),
    workdir: Path = typer.Option(
        Path("."),
        "--workdir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Directory used for deployment-method detection.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Check current version and update availability."""
    result = check_for_updates(current_version=__version__, method=method, cwd=workdir)
    payload = {
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "method": result.method,
        "update_available": result.update_available,
        "latest_git_tag": result.latest_git_tag,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    typer.echo(f"Current version: {result.current_version}")
    typer.echo(f"Recommended method: {result.method}")
    if result.latest_version is None:
        typer.echo("Latest PyPI version: unavailable")
    else:
        typer.echo(f"Latest PyPI version: {result.latest_version}")

    if result.method == "git":
        if result.latest_git_tag is None:
            typer.echo("Latest git tag: unavailable (create release tags or use --to)")
        else:
            typer.echo(f"Latest git tag: {result.latest_git_tag}")

    if result.update_available is True:
        typer.echo("Update available.")
    elif result.update_available is False:
        typer.echo("Already up to date.")
    else:
        typer.echo("Update availability could not be determined.")


@update_app.command("apply")
def self_update_apply(
    method: UpdateMethod = typer.Option(
        "auto",
        "--method",
        help="Update method to apply (auto, pip, git, docker).",
    ),
    to: str | None = typer.Option(
        None,
        "--to",
        help="Target version/ref (for pip or git).",
    ),
    workdir: Path = typer.Option(
        Path("."),
        "--workdir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Working directory for git/docker updates.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Run update commands without prompt."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print commands without executing."),
) -> None:
    """Apply update commands for Web2API."""
    resolved_method = detect_update_method(workdir) if method == "auto" else method
    if resolved_method not in {"pip", "git", "docker"}:
        typer.echo(f"Unsupported update method: {resolved_method}", err=True)
        raise typer.Exit(code=2)

    target = to
    if resolved_method == "git" and target is None:
        target = resolve_latest_git_tag(workdir)
        if target is None:
            typer.echo("No git tags found. Provide --to <tag_or_ref>.", err=True)
            raise typer.Exit(code=2)
        typer.echo(f"Using latest git tag: {target}")

    try:
        commands = build_update_commands(method=resolved_method, to_version=target)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    for command in commands:
        _print_command(command)

    _confirm_or_exit(
        f"Run {len(commands)} update command(s) via '{resolved_method}'?",
        yes=yes or dry_run,
    )

    try:
        apply_update_commands(commands, dry_run=dry_run)
    except FileNotFoundError as exc:
        typer.echo(f"Required executable not found: {exc.filename}", err=True)
        raise typer.Exit(code=1) from exc
    except subprocess.CalledProcessError as exc:
        typer.echo(f"Update command failed with exit code {exc.returncode}.", err=True)
        raise typer.Exit(code=exc.returncode) from exc

    typer.echo("Update command sequence completed.")
    typer.echo("Running post-update plugin diagnostics (`web2api plugins doctor`)...")
    try:
        plugins_doctor(
            slug=None,
            recipes_dir=None,
            json_output=False,
            run_healthchecks=True,
            allow_untrusted=False,
            healthcheck_timeout=15.0,
        )
    except typer.Exit as exc:
        if exc.exit_code not in {None, 0}:
            typer.echo(
                "Plugin diagnostics reported issues. Review output above.",
                err=True,
            )


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
