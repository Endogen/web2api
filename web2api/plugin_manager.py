"""Plugin listing, install/uninstall, and dependency management helpers."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from web2api.config import parse_recipe_config
from web2api.plugin import PluginConfig, build_plugin_payload, parse_plugin_config

logger = logging.getLogger(__name__)

DISABLED_MARKER = ".disabled"
MANIFEST_FILENAME = ".web2api_plugins.json"
_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent
_BUNDLED_ROOT = _PACKAGE_DIR / "bundled"
_PROJECT_RECIPES_DIR = _PROJECT_ROOT / "recipes"
_BUNDLED_RECIPES_DIR = _BUNDLED_ROOT / "recipes"
_PROJECT_CATALOG_PATH = _PROJECT_ROOT / "plugins" / "catalog.yaml"
_BUNDLED_CATALOG_PATH = _BUNDLED_ROOT / "plugins" / "catalog.yaml"
DEFAULT_CATALOG_PATH = (
    _PROJECT_CATALOG_PATH if _PROJECT_CATALOG_PATH.exists() else _BUNDLED_CATALOG_PATH
)
SourceType = Literal["local", "git", "catalog"]


@dataclass(slots=True)
class PluginEntry:
    """A recipe folder with optional plugin metadata."""

    slug: str
    folder: str
    path: Path
    enabled: bool
    has_recipe: bool
    plugin: PluginConfig | None
    error: str | None = None
    manifest_record: dict[str, Any] | None = None


def default_recipes_dir() -> Path:
    """Return default recipes directory path."""
    if _PROJECT_RECIPES_DIR.exists():
        return _PROJECT_RECIPES_DIR
    if _BUNDLED_RECIPES_DIR.exists():
        return _BUNDLED_RECIPES_DIR
    return _PROJECT_RECIPES_DIR


def resolve_recipes_dir(recipes_dir: Path | None) -> Path:
    """Resolve recipes directory from argument or environment."""
    if recipes_dir is not None:
        return recipes_dir
    env_value = os.environ.get("RECIPES_DIR")
    if env_value:
        return Path(env_value)
    return default_recipes_dir()


def manifest_path(recipes_dir: Path) -> Path:
    """Return the path to the plugin install-state manifest."""
    return recipes_dir / MANIFEST_FILENAME


def _empty_manifest() -> dict[str, Any]:
    return {"version": 1, "plugins": {}}


def load_manifest(recipes_dir: Path) -> dict[str, Any]:
    """Load plugin install-state manifest from recipes directory."""
    path = manifest_path(recipes_dir)
    if not path.exists():
        return _empty_manifest()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring invalid plugin manifest at %s", path)
        return _empty_manifest()

    if not isinstance(raw, dict):
        logger.warning("Ignoring malformed plugin manifest at %s", path)
        return _empty_manifest()

    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        logger.warning("Ignoring plugin manifest without 'plugins' mapping at %s", path)
        return _empty_manifest()

    version = raw.get("version")
    if not isinstance(version, int):
        version = 1
    return {"version": version, "plugins": plugins}


def save_manifest(recipes_dir: Path, manifest: dict[str, Any]) -> None:
    """Write plugin install-state manifest."""
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(recipes_dir)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def get_manifest_record(manifest: dict[str, Any], slug: str) -> dict[str, Any] | None:
    """Return manifest record for a slug if present."""
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return None
    record = plugins.get(slug)
    return record if isinstance(record, dict) else None


def record_plugin_install(
    recipes_dir: Path,
    *,
    slug: str,
    folder: str,
    source_type: SourceType,
    source: str,
    source_ref: str | None,
    source_subdir: str | None = None,
    trusted: bool,
) -> dict[str, Any]:
    """Upsert an installed-plugin record in manifest."""
    manifest = load_manifest(recipes_dir)
    plugins = manifest["plugins"]
    assert isinstance(plugins, dict)

    record = {
        "folder": folder,
        "source_type": source_type,
        "source": source,
        "source_ref": source_ref,
        "source_subdir": source_subdir,
        "trusted": trusted,
        "installed_at": datetime.now(UTC).isoformat(),
    }
    plugins[slug] = record
    save_manifest(recipes_dir, manifest)
    return record


def remove_manifest_record(recipes_dir: Path, slug: str) -> bool:
    """Delete a plugin record from manifest. Returns True if removed."""
    manifest = load_manifest(recipes_dir)
    plugins = manifest["plugins"]
    assert isinstance(plugins, dict)
    if slug not in plugins:
        return False
    del plugins[slug]
    save_manifest(recipes_dir, manifest)
    return True


def load_catalog(catalog_file: Path) -> dict[str, dict[str, Any]]:
    """Load plugin catalog metadata from YAML file."""
    if not catalog_file.exists():
        return {}

    raw_data = yaml.safe_load(catalog_file.read_text(encoding="utf-8"))
    if raw_data is None:
        return {}
    if not isinstance(raw_data, dict):
        raise ValueError(f"{catalog_file} must contain a YAML mapping")

    plugins = raw_data.get("plugins")
    if plugins is None:
        return {}
    if not isinstance(plugins, dict):
        raise ValueError(f"{catalog_file} field 'plugins' must be a mapping")

    catalog: dict[str, dict[str, Any]] = {}
    for raw_name, raw_entry in plugins.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{catalog_file} plugin '{name}' must be a mapping")
        source = raw_entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"{catalog_file} plugin '{name}' requires a non-empty string source")

        entry = {
            "source": source.strip(),
            "ref": raw_entry.get("ref"),
            "subdir": raw_entry.get("subdir"),
            "description": raw_entry.get("description"),
            "trusted": raw_entry.get("trusted"),
        }
        catalog[name] = entry
    return catalog


def is_disabled(recipe_dir: Path) -> bool:
    """Return ``True`` if a recipe directory is disabled."""
    return (recipe_dir / DISABLED_MARKER).exists()


def disable_recipe(recipe_dir: Path) -> None:
    """Mark a recipe as disabled."""
    marker = recipe_dir / DISABLED_MARKER
    marker.write_text("disabled by web2api cli\n", encoding="utf-8")


def enable_recipe(recipe_dir: Path) -> None:
    """Remove disabled marker if present."""
    marker = recipe_dir / DISABLED_MARKER
    if marker.exists():
        marker.unlink()


def _load_recipe_slug(recipe_dir: Path) -> tuple[str, str | None]:
    recipe_config_path = recipe_dir / "recipe.yaml"
    if not recipe_config_path.exists():
        return recipe_dir.name, "missing recipe.yaml"

    try:
        raw_data = yaml.safe_load(recipe_config_path.read_text(encoding="utf-8"))
        if raw_data is None:
            return recipe_dir.name, f"{recipe_config_path} is empty"
        if not isinstance(raw_data, dict):
            return recipe_dir.name, f"{recipe_config_path} must contain a YAML mapping"
        recipe_data = {str(key): value for key, value in raw_data.items()}
        config = parse_recipe_config(recipe_data, folder_name=recipe_dir.name)
        return config.slug, None
    except Exception as exc:  # noqa: BLE001
        return recipe_dir.name, str(exc)


def _load_recipe_config(recipe_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    recipe_config_path = recipe_dir / "recipe.yaml"
    if not recipe_config_path.exists():
        return None, f"missing recipe.yaml in {recipe_dir}"

    try:
        raw_data = yaml.safe_load(recipe_config_path.read_text(encoding="utf-8"))
        if raw_data is None:
            return None, f"{recipe_config_path} is empty"
        if not isinstance(raw_data, dict):
            return None, f"{recipe_config_path} must contain a YAML mapping"
        return {str(key): value for key, value in raw_data.items()}, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _load_plugin(recipe_dir: Path) -> tuple[PluginConfig | None, str | None]:
    plugin_path = recipe_dir / "plugin.yaml"
    if not plugin_path.exists():
        return None, None

    try:
        raw_data = yaml.safe_load(plugin_path.read_text(encoding="utf-8"))
        if raw_data is None:
            return None, f"{plugin_path} is empty"
        if not isinstance(raw_data, dict):
            return None, f"{plugin_path} must contain a YAML mapping"
        plugin_data = {str(key): value for key, value in raw_data.items()}
        return parse_plugin_config(plugin_data), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def discover_plugin_entries(recipes_dir: Path) -> list[PluginEntry]:
    """List recipe folders with plugin metadata and enablement state."""
    if not recipes_dir.exists() or not recipes_dir.is_dir():
        return []

    manifest = load_manifest(recipes_dir)
    entries: list[PluginEntry] = []
    seen_slugs: set[str] = set()
    for recipe_dir in sorted(path for path in recipes_dir.iterdir() if path.is_dir()):
        slug, recipe_error = _load_recipe_slug(recipe_dir)
        plugin, plugin_error = _load_plugin(recipe_dir)
        error = plugin_error or recipe_error
        manifest_record = get_manifest_record(manifest, slug)
        entries.append(
            PluginEntry(
                slug=slug,
                folder=recipe_dir.name,
                path=recipe_dir,
                enabled=not is_disabled(recipe_dir),
                has_recipe=recipe_error is None,
                plugin=plugin,
                error=error,
                manifest_record=manifest_record,
            )
        )
        seen_slugs.add(slug)

    plugins = manifest.get("plugins", {})
    if isinstance(plugins, dict):
        for slug, record in sorted(plugins.items()):
            if slug in seen_slugs or not isinstance(record, dict):
                continue
            folder = str(record.get("folder") or slug)
            orphan_path = recipes_dir / folder
            entries.append(
                PluginEntry(
                    slug=slug,
                    folder=folder,
                    path=orphan_path,
                    enabled=not is_disabled(orphan_path) if orphan_path.exists() else False,
                    has_recipe=False,
                    plugin=None,
                    error="manifest record exists but recipe directory is missing",
                    manifest_record=record,
                )
            )
    return entries


def find_plugin_entry(entries: list[PluginEntry], slug_or_folder: str) -> PluginEntry | None:
    """Locate plugin entry by slug or folder name."""
    for entry in entries:
        if entry.slug == slug_or_folder or entry.folder == slug_or_folder:
            return entry
    return None


def build_install_commands(
    plugin: PluginConfig,
    *,
    include_apt: bool = True,
    include_npm: bool = True,
    include_python: bool = True,
) -> list[list[str]]:
    """Build install commands from plugin metadata."""
    commands: list[list[str]] = []
    if include_apt and plugin.dependencies.apt_packages:
        commands.append(["apt-get", "update"])
        commands.append(["apt-get", "install", "-y", *plugin.dependencies.apt_packages])
    if include_npm and plugin.dependencies.npm_packages:
        commands.append(["npm", "install", "-g", *plugin.dependencies.npm_packages])
    if include_python and plugin.dependencies.python_packages:
        commands.append(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *plugin.dependencies.python_packages,
            ]
        )
    return commands


def build_dockerfile_snippet(commands: list[list[str]]) -> str:
    """Render Dockerfile RUN lines for install commands."""
    if not commands:
        return "# No plugin dependency install steps."
    rendered = ["# Add these lines to your Dockerfile for plugin dependencies:"]
    for command in commands:
        rendered.append(f"RUN {shlex.join(command)}")
    return "\n".join(rendered)


def run_commands(
    commands: list[list[str]],
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Execute commands sequentially with optional dry-run mode."""
    executor = runner or subprocess.run
    for command in commands:
        logger.info("Executing: %s", " ".join(command))
        if dry_run:
            continue
        executor(command, check=True, text=True)


def plugin_status_payload(plugin: PluginConfig, *, app_version: str) -> dict[str, object]:
    """Build plugin payload with computed readiness status."""
    return build_plugin_payload(plugin, current_web2api_version=app_version)


def run_healthcheck(
    plugin: PluginConfig,
    *,
    timeout_seconds: float = 15.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run plugin healthcheck command and return structured status."""
    healthcheck = plugin.healthcheck
    if healthcheck is None:
        return {"defined": False, "ran": False, "ok": None}

    command = healthcheck.command
    result_payload: dict[str, Any] = {
        "defined": True,
        "ran": not dry_run,
        "ok": None if dry_run else False,
        "command": command,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }

    if dry_run:
        return result_payload

    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        result_payload["stderr"] = f"command not found: {exc.filename}"
        return result_payload
    except subprocess.TimeoutExpired:
        result_payload["stderr"] = f"healthcheck timed out after {timeout_seconds}s"
        return result_payload

    result_payload["exit_code"] = proc.returncode
    result_payload["stdout"] = proc.stdout.strip()
    result_payload["stderr"] = proc.stderr.strip()
    result_payload["ok"] = proc.returncode == 0
    return result_payload


def resolve_source_type(source: str) -> SourceType:
    """Resolve plugin source type from source value."""
    if Path(source).expanduser().exists():
        return "local"
    return "git"


@contextmanager
def checkout_source(
    source: str,
    *,
    source_ref: str | None = None,
    source_type: SourceType | None = None,
) -> Path:
    """Yield a local checkout path for a source value."""
    resolved_type = source_type or resolve_source_type(source)
    if resolved_type == "local":
        yield Path(source).expanduser().resolve()
        return

    with tempfile.TemporaryDirectory(prefix="web2api-plugin-src-") as tmp_dir:
        target = Path(tmp_dir) / "repo"
        clone_cmd = ["git", "clone", source, str(target)]
        subprocess.run(clone_cmd, check=True, text=True)
        if source_ref is not None:
            subprocess.run(
                ["git", "-C", str(target), "checkout", source_ref],
                check=True,
                text=True,
            )
        yield target


def resolve_recipe_source_dir(source_root: Path, subdir: str | None = None) -> Path:
    """Resolve recipe directory inside source root."""
    if subdir is not None:
        recipe_dir = (source_root / subdir).resolve()
        if not recipe_dir.exists() or not recipe_dir.is_dir():
            raise ValueError(f"source subdir does not exist or is not a directory: {recipe_dir}")
        if not (recipe_dir / "recipe.yaml").exists():
            raise ValueError(f"source subdir does not contain recipe.yaml: {recipe_dir}")
        return recipe_dir

    if (source_root / "recipe.yaml").exists():
        return source_root

    candidates = [
        child
        for child in sorted(source_root.iterdir())
        if child.is_dir() and (child / "recipe.yaml").exists()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"no recipe.yaml found in source: {source_root}")
    candidate_names = ", ".join(c.name for c in candidates)
    raise ValueError(
        f"source contains multiple recipes; pass --subdir. Candidates: {candidate_names}"
    )


def load_source_recipe_slug(source_recipe_dir: Path) -> str:
    """Load and validate slug from a source recipe directory."""
    recipe_data, error = _load_recipe_config(source_recipe_dir)
    if recipe_data is None:
        raise ValueError(error or f"invalid source recipe in {source_recipe_dir}")
    config = parse_recipe_config(recipe_data)
    return config.slug


def copy_recipe_into_recipes_dir(
    source_recipe_dir: Path,
    recipes_dir: Path,
    *,
    overwrite: bool = False,
) -> tuple[str, Path]:
    """Copy recipe directory into recipes directory using slug as folder name."""
    slug = load_source_recipe_slug(source_recipe_dir)
    destination = recipes_dir / slug

    if destination.exists():
        if not overwrite:
            raise ValueError(f"destination recipe already exists: {destination}")
        shutil.rmtree(destination)

    shutil.copytree(source_recipe_dir, destination)
    disabled_marker = destination / DISABLED_MARKER
    if disabled_marker.exists():
        disabled_marker.unlink()
    return slug, destination
