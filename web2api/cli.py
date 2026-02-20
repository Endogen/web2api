"""Command line interface for Web2API recipe and self-update workflows."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

import typer

from web2api import __version__
from web2api.recipe_manager import (
    SourceType,
    build_dockerfile_snippet,
    build_entry_payload,
    build_install_commands,
    default_catalog_path,
    default_catalog_ref,
    default_catalog_source,
    disable_recipe,
    discover_recipe_entries,
    enable_recipe,
    entry_is_trusted,
    find_recipe_entry,
    get_manifest_record,
    install_recipe_from_source,
    load_manifest,
    metadata_status_payload,
    remove_manifest_record,
    resolve_catalog_recipes,
    resolve_managed_recipe_source,
    resolve_recipe_folder,
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
recipes_app = typer.Typer(no_args_is_help=True, help="Manage recipes and optional metadata.")
catalog_app = typer.Typer(no_args_is_help=True, help="Recipe catalog source commands.")
self_app = typer.Typer(no_args_is_help=True, help="Manage Web2API installation.")
update_app = typer.Typer(no_args_is_help=True, help="Self-update commands.")

app.add_typer(recipes_app, name="recipes")
recipes_app.add_typer(catalog_app, name="catalog")
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


def _resolve_catalog_options(
    *,
    catalog_source: str | None,
    catalog_ref: str | None,
    catalog_path: str | None,
) -> tuple[str, str | None, str | None]:
    resolved_source = (
        catalog_source.strip()
        if isinstance(catalog_source, str) and catalog_source.strip()
        else default_catalog_source()
    )
    resolved_ref = catalog_ref if catalog_ref is not None else default_catalog_ref()
    resolved_path = catalog_path if catalog_path is not None else default_catalog_path()
    return resolved_source, resolved_ref, resolved_path


def _add_recipe_from_source(
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

    if resolved_source_type == "git":
        _confirm_or_exit(
            f"Fetch recipe source from git repository '{source}'?",
            yes=yes,
        )

    return install_recipe_from_source(
        source=source,
        recipes_dir=recipes_dir,
        source_ref=source_ref,
        source_subdir=source_subdir,
        trusted=trusted,
        overwrite=overwrite,
        record_source_type=record_source_type,
        expected_slug=expected_slug,
    )


@recipes_app.command("list")
def recipes_list(
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List recipes, metadata readiness, and install-state metadata."""
    target_dir = _recipes_dir_option(recipes_dir)
    entries = discover_recipe_entries(target_dir)

    payload = [build_entry_payload(entry, app_version=__version__) for entry in entries]

    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not payload:
        typer.echo(f"No recipe folders found in {target_dir}.")
        return

    for item in payload:
        metadata_block = item["plugin"]
        metadata_ready = "-"
        metadata_version = "-"
        if isinstance(metadata_block, dict):
            status = metadata_block.get("status", {})
            if isinstance(status, dict):
                metadata_ready = str(status.get("ready"))
            metadata_version = str(metadata_block.get("version"))

        state = "enabled" if item["enabled"] else "disabled"
        managed = "managed" if item["managed"] else "unmanaged"
        trusted = "trusted" if item["trusted"] else "untrusted"
        source_label = str(item["source_type"] or "-")
        origin = str(item["origin"])
        line = (
            f"{item['slug']}: state={state}, {managed}, {trusted}, origin={origin}, "
            f"source={source_label}, "
            f"metadata={metadata_version}, ready={metadata_ready}, path={item['path']}"
        )
        typer.echo(line)
        if item["error"]:
            typer.echo(f"  error: {item['error']}")


@recipes_app.command("doctor")
def recipes_doctor(
    slug: str | None = typer.Argument(default=None, help="Recipe slug (optional)."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
    run_healthchecks: bool = typer.Option(
        True,
        "--run-healthchecks/--no-run-healthchecks",
        help="Run metadata healthcheck commands when configured.",
    ),
    allow_untrusted: bool = typer.Option(
        False,
        "--allow-untrusted",
        help="Allow executing checks for recipes marked as untrusted.",
    ),
    healthcheck_timeout: float = typer.Option(
        15.0,
        "--healthcheck-timeout",
        min=1.0,
        help="Timeout in seconds for each metadata healthcheck command.",
    ),
) -> None:
    """Show recipe metadata readiness details and optional healthcheck results."""
    entries = discover_recipe_entries(_recipes_dir_option(recipes_dir))

    if slug is not None:
        selected = find_recipe_entry(entries, slug)
        entries = [selected] if selected is not None else []
    if not entries:
        typer.echo("No matching recipes found.", err=True)
        raise typer.Exit(code=1)

    report: list[dict[str, object]] = []
    for entry in entries:
        trusted = entry_is_trusted(entry.manifest_record)
        status_payload = None
        if entry.plugin is not None:
            status_payload = metadata_status_payload(entry.plugin, app_version=__version__)

        healthcheck_payload: dict[str, Any] | None = None
        if entry.plugin is not None and run_healthchecks:
            if not trusted and not allow_untrusted:
                if entry.plugin.healthcheck is not None:
                    healthcheck_payload = {
                        "defined": True,
                        "ran": False,
                        "ok": None,
                        "skipped": "untrusted recipe; pass --allow-untrusted to run healthcheck",
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
            metadata_block = item["plugin"]
            if not isinstance(metadata_block, dict):
                typer.echo(f"{slug_value}: no plugin.yaml ({trusted})")
                continue
            status = metadata_block.get("status")
            if not isinstance(status, dict):
                typer.echo(f"{slug_value}: metadata status unavailable ({trusted})")
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
        metadata_block = item["plugin"]
        if isinstance(metadata_block, dict):
            status = metadata_block.get("status")
            if isinstance(status, dict) and status.get("ready") is False:
                failed = True

        health = item.get("healthcheck")
        if isinstance(health, dict) and health.get("defined") and health.get("ok") is False:
            failed = True
    if failed:
        raise typer.Exit(code=1)


@recipes_app.command("install")
def recipes_install(
    slug: str = typer.Argument(help="Recipe slug to install dependencies for."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
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
        help="Allow command execution for recipes marked as untrusted.",
    ),
    target: Literal["host", "docker"] = typer.Option(
        "host",
        "--target",
        help="Install target: host executes commands, docker prints Dockerfile RUN snippet.",
    ),
) -> None:
    """Install dependencies declared in plugin.yaml."""
    entries = discover_recipe_entries(_recipes_dir_option(recipes_dir))
    entry = find_recipe_entry(entries, slug)
    if entry is None:
        typer.echo(f"Recipe '{slug}' was not found.", err=True)
        raise typer.Exit(code=1)
    if entry.plugin is None:
        typer.echo(f"Recipe '{entry.slug}' has no plugin.yaml.", err=True)
        raise typer.Exit(code=1)

    trusted = entry_is_trusted(entry.manifest_record)
    if not trusted and not allow_untrusted:
        typer.echo(
            f"Recipe '{entry.slug}' is marked untrusted. "
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
        typer.echo("No install actions are defined for this recipe metadata.")
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

    payload = metadata_status_payload(entry.plugin, app_version=__version__)
    status = payload["status"]
    typer.echo(f"Recipe '{entry.slug}' metadata ready={status['ready']}")
    checks = status["checks"]
    for name in ("env", "commands", "python"):
        missing = checks[name]["missing"]
        if missing:
            typer.echo(f"  missing {name}: {', '.join(missing)}")


@recipes_app.command("add")
def recipes_add(
    source: str = typer.Argument(help="Recipe source path or git URL."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to install into (defaults to RECIPES_DIR or ~/.web2api/recipes).",
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
    """Install a recipe from local path or git source."""
    target_dir = _recipes_dir_option(recipes_dir)
    source_type = resolve_source_type(source)
    trusted_value = trusted or source_type == "local"

    try:
        slug, recorded_type = _add_recipe_from_source(
            source=source,
            source_ref=source_ref,
            source_subdir=source_subdir,
            recipes_dir=target_dir,
            trusted=trusted_value,
            overwrite=overwrite,
            yes=yes,
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to install recipe from source: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    trust_label = "trusted" if trusted_value else "untrusted"
    typer.echo(
        f"Installed recipe '{slug}' from {recorded_type} source ({trust_label}). "
        f"Run `web2api recipes install {slug}` to install dependencies."
    )


@recipes_app.command("update")
def recipes_update(
    slug: str = typer.Argument(help="Recipe slug to update from recorded source."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
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
    """Update a managed recipe from its recorded source."""
    target_dir = _recipes_dir_option(recipes_dir)
    entries = discover_recipe_entries(target_dir)
    entry = find_recipe_entry(entries, slug)
    manifest = load_manifest(target_dir)
    manifest_record = get_manifest_record(manifest, slug)

    if manifest_record is None:
        typer.echo(
            f"Recipe '{slug}' is not tracked in manifest. "
            "Use `web2api recipes add` to install managed recipes.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        managed_source = resolve_managed_recipe_source(manifest_record, slug=slug)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    source = managed_source.source
    source_ref_value = source_ref if source_ref is not None else None
    if source_ref_value is None:
        source_ref_value = managed_source.source_ref
    source_subdir_value = source_subdir if source_subdir is not None else None
    if source_subdir_value is None:
        source_subdir_value = managed_source.source_subdir

    trusted = managed_source.trusted
    record_source_type = managed_source.source_type

    was_disabled = entry is not None and not entry.enabled
    _confirm_or_exit(
        f"Update recipe '{slug}' from source '{source}'?",
        yes=yes,
    )

    try:
        updated_slug, recorded_type = _add_recipe_from_source(
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
        typer.echo(f"Failed to update recipe '{slug}': {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if was_disabled:
        disable_recipe(target_dir / updated_slug)

    trust_label = "trusted" if trusted else "untrusted"
    typer.echo(f"Updated recipe '{updated_slug}' from {recorded_type} source ({trust_label}).")


@recipes_app.command("uninstall")
def recipes_uninstall(
    slug: str = typer.Argument(help="Recipe slug to uninstall."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Uninstall without prompt."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow uninstalling recipes not tracked in manifest.",
    ),
    delete_files: bool = typer.Option(
        True,
        "--delete-files/--keep-files",
        help="Delete recipe directory on uninstall.",
    ),
) -> None:
    """Uninstall a recipe and remove its manifest record."""
    target_dir = _recipes_dir_option(recipes_dir)
    entries = discover_recipe_entries(target_dir)
    entry = find_recipe_entry(entries, slug)
    manifest = load_manifest(target_dir)
    manifest_record = get_manifest_record(manifest, slug)

    if entry is None and manifest_record is None:
        typer.echo(f"Recipe '{slug}' was not found in recipes or manifest.", err=True)
        raise typer.Exit(code=1)

    if manifest_record is None and not force:
        typer.echo(
            f"Recipe '{slug}' is not tracked in manifest. "
            "Use `recipes disable` or pass --force to remove files anyway.",
            err=True,
        )
        raise typer.Exit(code=1)

    folder = resolve_recipe_folder(slug=slug, entry=entry, manifest_record=manifest_record)
    recipe_path = target_dir / folder
    _confirm_or_exit(
        f"Uninstall recipe '{slug}' (folder: {recipe_path})?",
        yes=yes,
    )

    if delete_files and recipe_path.exists():
        shutil.rmtree(recipe_path)
        typer.echo(f"Deleted recipe directory: {recipe_path}")

    removed = remove_manifest_record(target_dir, slug)
    if removed:
        typer.echo(f"Removed manifest record for '{slug}'.")

    if not removed and not recipe_path.exists():
        typer.echo(f"Recipe '{slug}' was not installed or already removed.")
    else:
        typer.echo(f"Uninstalled recipe '{slug}'.")


@recipes_app.command("enable")
def recipes_enable(
    slug: str = typer.Argument(help="Recipe slug to enable."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
    ),
) -> None:
    """Enable a recipe by removing .disabled marker."""
    entries = discover_recipe_entries(_recipes_dir_option(recipes_dir))
    entry = find_recipe_entry(entries, slug)
    if entry is None:
        typer.echo(f"Recipe '{slug}' was not found.", err=True)
        raise typer.Exit(code=1)
    if entry.enabled:
        typer.echo(f"Recipe '{entry.slug}' is already enabled.")
        return
    enable_recipe(entry.path)
    typer.echo(f"Enabled recipe '{entry.slug}'.")


@recipes_app.command("disable")
def recipes_disable(
    slug: str = typer.Argument(help="Recipe slug to disable."),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to inspect (defaults to RECIPES_DIR or ~/.web2api/recipes).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Disable without prompt."),
) -> None:
    """Disable a recipe by writing .disabled marker."""
    entries = discover_recipe_entries(_recipes_dir_option(recipes_dir))
    entry = find_recipe_entry(entries, slug)
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
def recipes_catalog_list(
    catalog_source: str | None = typer.Option(
        None,
        "--catalog-source",
        help=(
            "Catalog source path or git URL. Defaults to WEB2API_RECIPE_CATALOG_SOURCE "
            "or the default official recipe repository."
        ),
    ),
    catalog_ref: str | None = typer.Option(
        None,
        "--catalog-ref",
        help="Optional git branch/tag/commit for --catalog-source.",
    ),
    catalog_path: str | None = typer.Option(
        None,
        "--catalog-path",
        help="Catalog file path inside --catalog-source (default: catalog.yaml).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List available recipe catalog entries."""
    try:
        source_value, ref_value, path_value = _resolve_catalog_options(
            catalog_source=catalog_source,
            catalog_ref=catalog_ref,
            catalog_path=catalog_path,
        )
        catalog = resolve_catalog_recipes(
            catalog_source=source_value,
            catalog_ref=ref_value,
            catalog_path=path_value,
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to load catalog: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        payload = {
            name: {
                "slug": spec.slug,
                "source": spec.source,
                "ref": spec.source_ref,
                "subdir": spec.source_subdir,
                "description": spec.description,
                "trusted": spec.trusted,
            }
            for name, spec in sorted(catalog.items())
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not catalog:
        typer.echo(f"No catalog entries found in {source_value}.")
        return

    typer.echo(f"Catalog source: {source_value}")
    if ref_value:
        typer.echo(f"Catalog ref: {ref_value}")
    if path_value:
        typer.echo(f"Catalog path: {path_value}")
    for name, spec in sorted(catalog.items()):
        trusted_label = "trusted" if spec.trusted is True else "untrusted"
        typer.echo(
            f"{name}: slug={spec.slug}, source={spec.source}, "
            f"subdir={spec.source_subdir or '-'}, {trusted_label}"
        )
        if spec.description:
            typer.echo(f"  {spec.description}")


@catalog_app.command("add")
def recipes_catalog_add(
    name: str = typer.Argument(help="Catalog recipe entry name to install."),
    catalog_source: str | None = typer.Option(
        None,
        "--catalog-source",
        help=(
            "Catalog source path or git URL. Defaults to WEB2API_RECIPE_CATALOG_SOURCE "
            "or the default official recipe repository."
        ),
    ),
    catalog_ref: str | None = typer.Option(
        None,
        "--catalog-ref",
        help="Optional git branch/tag/commit for --catalog-source.",
    ),
    catalog_path: str | None = typer.Option(
        None,
        "--catalog-path",
        help="Catalog file path inside --catalog-source (default: catalog.yaml).",
    ),
    recipes_dir: Path | None = typer.Option(
        None,
        "--recipes-dir",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Recipe directory to install into (defaults to RECIPES_DIR or ~/.web2api/recipes).",
    ),
    trusted: bool = typer.Option(
        False,
        "--trusted",
        help="Override catalog trust and mark source as trusted.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing recipe folder."),
    yes: bool = typer.Option(False, "--yes", help="Continue without interactive confirmation."),
) -> None:
    """Install a recipe from configured catalog entry."""
    try:
        source_value, ref_value, path_value = _resolve_catalog_options(
            catalog_source=catalog_source,
            catalog_ref=catalog_ref,
            catalog_path=catalog_path,
        )
        catalog = resolve_catalog_recipes(
            catalog_source=source_value,
            catalog_ref=ref_value,
            catalog_path=path_value,
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to load catalog: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    spec = catalog.get(name)
    if spec is None:
        typer.echo(f"Catalog entry '{name}' was not found in {source_value}.", err=True)
        raise typer.Exit(code=1)

    trusted_value = trusted or bool(spec.trusted)

    target_dir = _recipes_dir_option(recipes_dir)

    try:
        slug, _ = _add_recipe_from_source(
            source=spec.source,
            source_ref=spec.source_ref,
            source_subdir=spec.source_subdir,
            recipes_dir=target_dir,
            trusted=trusted_value,
            overwrite=overwrite,
            yes=yes,
            record_source_type="catalog",
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        typer.echo(f"Failed to install catalog recipe '{name}': {exc}", err=True)
        raise typer.Exit(code=1) from exc

    trust_label = "trusted" if trusted_value else "untrusted"
    typer.echo(f"Installed catalog recipe '{name}' as slug '{slug}' ({trust_label}).")


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
    typer.echo("Running post-update recipe diagnostics (`web2api recipes doctor`)...")
    try:
        recipes_doctor(
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
                "Recipe diagnostics reported issues. Review output above.",
                err=True,
            )


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
