"""Recipe listing, install/uninstall, and dependency metadata helpers."""

from __future__ import annotations

import json
import logging
import os
import re
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
from urllib.parse import quote

import yaml

from web2api.config import parse_recipe_config
from web2api.plugin import PluginConfig, build_plugin_payload, parse_plugin_config

logger = logging.getLogger(__name__)

DISABLED_MARKER = ".disabled"
MANIFEST_FILENAME = ".web2api_recipes.json"
OFFICIAL_RECIPES_REPO_URL = "https://github.com/Endogen/web2api-recipes.git"
DEFAULT_RECIPES_HOME = Path.home() / ".web2api" / "recipes"
CATALOG_SOURCE_ENV = "WEB2API_RECIPE_CATALOG_SOURCE"
CATALOG_REF_ENV = "WEB2API_RECIPE_CATALOG_REF"
CATALOG_PATH_ENV = "WEB2API_RECIPE_CATALOG_PATH"
SourceType = Literal["local", "git", "catalog"]
CATALOG_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(slots=True)
class RecipeEntry:
    """A recipe folder with optional dependency metadata."""

    slug: str
    folder: str
    path: Path
    enabled: bool
    has_recipe: bool
    plugin: PluginConfig | None
    error: str | None = None
    manifest_record: dict[str, Any] | None = None


@dataclass(slots=True)
class CatalogRecipeSpec:
    """Install-ready recipe details resolved from catalog source."""

    name: str
    slug: str
    source: str
    source_ref: str | None
    source_subdir: str | None
    description: str | None
    trusted: bool | None
    docs_url: str | None
    requires_env: list[str]


@dataclass(slots=True)
class ManagedRecipeSource:
    """Validated recipe source details loaded from a manifest record."""

    source: str
    source_ref: str | None
    source_subdir: str | None
    trusted: bool
    source_type: SourceType | None


def default_recipes_dir() -> Path:
    """Return default recipes directory path."""
    return DEFAULT_RECIPES_HOME


def default_catalog_source() -> str:
    """Return catalog source URL/path used for repo browsing."""
    configured = os.environ.get(CATALOG_SOURCE_ENV)
    if configured is not None and configured.strip():
        return configured.strip()
    return OFFICIAL_RECIPES_REPO_URL


def default_catalog_ref() -> str | None:
    """Return optional source ref used for catalog source checkout."""
    raw = os.environ.get(CATALOG_REF_ENV)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def default_catalog_path() -> str:
    """Return catalog file path inside the catalog source."""
    raw = os.environ.get(CATALOG_PATH_ENV)
    if raw is None:
        return "catalog.yaml"
    value = raw.strip()
    return value or "catalog.yaml"


def resolve_recipes_dir(recipes_dir: Path | None) -> Path:
    """Resolve recipes directory from argument or environment."""
    if recipes_dir is not None:
        return recipes_dir
    env_value = os.environ.get("RECIPES_DIR")
    if env_value:
        return Path(env_value)
    return default_recipes_dir()


def manifest_path(recipes_dir: Path) -> Path:
    """Return the path to the recipe install-state manifest."""
    return recipes_dir / MANIFEST_FILENAME


def _empty_manifest() -> dict[str, Any]:
    return {"version": 1, "recipes": {}}


def load_manifest(recipes_dir: Path) -> dict[str, Any]:
    """Load recipe install-state manifest from recipes directory."""
    path = manifest_path(recipes_dir)
    if not path.exists():
        return _empty_manifest()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring invalid recipe manifest at %s", path)
        return _empty_manifest()

    if not isinstance(raw, dict):
        logger.warning("Ignoring malformed recipe manifest at %s", path)
        return _empty_manifest()

    recipes = raw.get("recipes")
    if not isinstance(recipes, dict):
        logger.warning("Ignoring recipe manifest without 'recipes' mapping at %s", path)
        return _empty_manifest()

    version = raw.get("version")
    if not isinstance(version, int):
        version = 1
    return {"version": version, "recipes": recipes}


def save_manifest(recipes_dir: Path, manifest: dict[str, Any]) -> None:
    """Write recipe install-state manifest."""
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(recipes_dir)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def get_manifest_record(manifest: dict[str, Any], slug: str) -> dict[str, Any] | None:
    """Return manifest record for a slug if present."""
    recipes = manifest.get("recipes")
    if not isinstance(recipes, dict):
        return None
    record = recipes.get(slug)
    return record if isinstance(record, dict) else None


def source_type_from_manifest_record(record: dict[str, Any]) -> SourceType | None:
    """Return a normalized source type from manifest record."""
    value = record.get("source_type")
    if value in {"local", "git", "catalog"}:
        return value
    return None


def entry_is_trusted(entry_record: dict[str, Any] | None) -> bool:
    """Return trust flag from manifest record; defaults to trusted."""
    if isinstance(entry_record, dict) and isinstance(entry_record.get("trusted"), bool):
        return bool(entry_record["trusted"])
    return True


def recipe_origin(source_type: str | None) -> str:
    """Return normalized recipe origin from source type."""
    if isinstance(source_type, str) and source_type in {"catalog", "git", "local"}:
        return source_type
    return "unmanaged"


def resolve_recipe_folder(
    *,
    slug: str,
    entry: RecipeEntry | None,
    manifest_record: dict[str, Any] | None,
) -> str:
    """Resolve recipe folder name for uninstall operations."""
    if entry is not None:
        return entry.folder
    if isinstance(manifest_record, dict):
        return str(manifest_record.get("folder") or slug)
    return slug


def resolve_managed_recipe_source(
    manifest_record: dict[str, Any],
    *,
    slug: str,
) -> ManagedRecipeSource:
    """Validate and normalize managed source fields from manifest record."""
    source_raw = manifest_record.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise ValueError(f"recipe '{slug}' has no source record")
    source = source_raw.strip()

    source_ref = None
    if isinstance(manifest_record.get("source_ref"), str):
        source_ref = str(manifest_record["source_ref"])

    source_subdir = None
    if isinstance(manifest_record.get("source_subdir"), str):
        source_subdir = str(manifest_record["source_subdir"])

    return ManagedRecipeSource(
        source=source,
        source_ref=source_ref,
        source_subdir=source_subdir,
        trusted=entry_is_trusted(manifest_record),
        source_type=source_type_from_manifest_record(manifest_record),
    )


def build_entry_payload(entry: RecipeEntry, *, app_version: str) -> dict[str, Any]:
    """Serialize recipe entry metadata for CLI/API output."""
    metadata_payload = None
    if entry.plugin is not None:
        metadata_payload = build_plugin_payload(
            entry.plugin,
            current_web2api_version=app_version,
        )

    source = None
    source_type = None
    managed = False
    trusted = True
    if isinstance(entry.manifest_record, dict):
        managed = True
        trusted = entry_is_trusted(entry.manifest_record)
        source_raw = entry.manifest_record.get("source")
        source_type_raw = entry.manifest_record.get("source_type")
        if source_raw is not None:
            source = str(source_raw)
        if source_type_raw is not None:
            source_type = str(source_type_raw)

    return {
        "slug": entry.slug,
        "folder": entry.folder,
        "enabled": entry.enabled,
        "has_recipe": entry.has_recipe,
        "managed": managed,
        "trusted": trusted,
        "source_type": source_type,
        "source": source,
        "origin": recipe_origin(source_type),
        "error": entry.error,
        "plugin": metadata_payload,
        "path": str(entry.path),
    }


def record_recipe_install(
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
    """Upsert an installed recipe record in manifest."""
    manifest = load_manifest(recipes_dir)
    recipes = manifest["recipes"]
    assert isinstance(recipes, dict)

    record = {
        "folder": folder,
        "source_type": source_type,
        "source": source,
        "source_ref": source_ref,
        "source_subdir": source_subdir,
        "trusted": trusted,
        "installed_at": datetime.now(UTC).isoformat(),
    }
    recipes[slug] = record
    save_manifest(recipes_dir, manifest)
    return record


def remove_manifest_record(recipes_dir: Path, slug: str) -> bool:
    """Delete a recipe record from manifest. Returns True if removed."""
    manifest = load_manifest(recipes_dir)
    recipes = manifest["recipes"]
    assert isinstance(recipes, dict)
    if slug not in recipes:
        return False
    del recipes[slug]
    save_manifest(recipes_dir, manifest)
    return True


def _optional_nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_catalog_requires_env(
    value: Any,
    *,
    catalog_file: Path,
    recipe_name: str,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"{catalog_file} recipe '{recipe_name}' field 'requires_env' must be a list"
        )
    normalized: list[str] = []
    for raw_name in value:
        if not isinstance(raw_name, str):
            raise ValueError(
                f"{catalog_file} recipe '{recipe_name}' field 'requires_env' must contain strings"
            )
        env_name = raw_name.strip()
        if not env_name:
            raise ValueError(
                f"{catalog_file} recipe '{recipe_name}' field 'requires_env' must not contain empty entries"
            )
        if not CATALOG_ENV_NAME_PATTERN.match(env_name):
            raise ValueError(
                f"{catalog_file} recipe '{recipe_name}' field 'requires_env' has invalid env name {env_name!r}"
            )
        if env_name not in normalized:
            normalized.append(env_name)
    return normalized


def _github_repo_from_source(source: str) -> str | None:
    normalized = source.strip()
    if normalized.startswith("https://github.com/"):
        path = normalized.removeprefix("https://github.com/")
    elif normalized.startswith("http://github.com/"):
        path = normalized.removeprefix("http://github.com/")
    elif normalized.startswith("git@github.com:"):
        path = normalized.removeprefix("git@github.com:")
    elif normalized.startswith("ssh://git@github.com/"):
        path = normalized.removeprefix("ssh://git@github.com/")
    else:
        return None

    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None
    owner = segments[0]
    repo = segments[1]
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def _derive_github_readme_url(
    *,
    source: str,
    source_ref: str | None,
    source_subdir: str | None,
) -> str | None:
    repo = _github_repo_from_source(source)
    if repo is None:
        return None
    ref = quote(source_ref or "HEAD", safe="")
    cleaned_subdir = str(source_subdir or "").strip("/")
    encoded_subdir = "/".join(quote(part, safe="") for part in cleaned_subdir.split("/") if part)
    if encoded_subdir:
        return f"https://github.com/{repo}/blob/{ref}/{encoded_subdir}/README.md"
    return f"https://github.com/{repo}/blob/{ref}/README.md"


def load_catalog(catalog_file: Path) -> dict[str, dict[str, Any]]:
    """Load recipe catalog metadata from YAML file."""
    if not catalog_file.exists():
        return {}

    raw_data = yaml.safe_load(catalog_file.read_text(encoding="utf-8"))
    if raw_data is None:
        return {}
    if not isinstance(raw_data, dict):
        raise ValueError(f"{catalog_file} must contain a YAML mapping")

    recipes = raw_data.get("recipes")
    if recipes is None:
        return {}
    if not isinstance(recipes, dict):
        raise ValueError(f"{catalog_file} field 'recipes' must be a mapping")

    catalog: dict[str, dict[str, Any]] = {}
    for raw_name, raw_entry in recipes.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{catalog_file} recipe '{name}' must be a mapping")
        source = raw_entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"{catalog_file} recipe '{name}' requires a non-empty string source")

        entry = {
            "source": source.strip(),
            "ref": raw_entry.get("ref"),
            "subdir": raw_entry.get("subdir"),
            "slug": raw_entry.get("slug"),
            "description": raw_entry.get("description"),
            "trusted": raw_entry.get("trusted"),
            "docs_url": _optional_nonempty_string(raw_entry.get("docs_url"))
            or _optional_nonempty_string(raw_entry.get("readme_url")),
            "requires_env": _normalize_catalog_requires_env(
                raw_entry.get("requires_env"),
                catalog_file=catalog_file,
                recipe_name=name,
            ),
        }
        catalog[name] = entry
    return catalog


def _looks_like_remote_source(value: str) -> bool:
    return "://" in value or value.startswith("git@")


def _resolve_local_catalog_source(raw_source: str, catalog_file: Path) -> str:
    candidate = Path(raw_source).expanduser()
    if _looks_like_remote_source(raw_source):
        return raw_source
    if candidate.is_absolute():
        return str(candidate)
    return str((catalog_file.parent / candidate).resolve())


def resolve_catalog_recipes(
    *,
    catalog_source: str | None = None,
    catalog_ref: str | None = None,
    catalog_path: str | None = None,
) -> dict[str, CatalogRecipeSpec]:
    """Resolve install-ready recipe specs from catalog source."""
    source_value = catalog_source or default_catalog_source()
    ref_value = catalog_ref if catalog_ref is not None else default_catalog_ref()
    path_value = catalog_path or default_catalog_path()

    source_type = resolve_source_type(source_value)
    if source_type == "local":
        source_path = Path(source_value).expanduser().resolve()
        catalog_file = source_path if source_path.is_file() else source_path / path_value
        catalog = load_catalog(catalog_file)

        resolved: dict[str, CatalogRecipeSpec] = {}
        for name, entry in sorted(catalog.items()):
            raw_source = str(entry.get("source") or "").strip()
            source_ref = _optional_nonempty_string(entry.get("ref"))
            source_subdir = _optional_nonempty_string(entry.get("subdir"))
            slug = _optional_nonempty_string(entry.get("slug")) or name
            description = _optional_nonempty_string(entry.get("description"))
            docs_url = _optional_nonempty_string(entry.get("docs_url"))
            requires_env = entry.get("requires_env")
            requires_env_list = requires_env if isinstance(requires_env, list) else []
            resolved_source = _resolve_local_catalog_source(raw_source, catalog_file)
            resolved_docs_url = docs_url or _derive_github_readme_url(
                source=resolved_source,
                source_ref=source_ref,
                source_subdir=source_subdir,
            )
            resolved[name] = CatalogRecipeSpec(
                name=name,
                slug=slug,
                source=resolved_source,
                source_ref=source_ref,
                source_subdir=source_subdir,
                description=description,
                trusted=entry.get("trusted") if isinstance(entry.get("trusted"), bool) else None,
                docs_url=resolved_docs_url,
                requires_env=[str(item) for item in requires_env_list],
            )
        return resolved

    with checkout_source(
        source_value,
        source_ref=ref_value,
        source_type="git",
        sparse_paths=[path_value],
    ) as source_root:
        catalog_file = source_root / path_value
        catalog = load_catalog(catalog_file)

    resolved = {}
    for name, entry in sorted(catalog.items()):
        raw_source = str(entry.get("source") or "").strip()
        raw_entry_ref = _optional_nonempty_string(entry.get("ref"))
        raw_entry_subdir = _optional_nonempty_string(entry.get("subdir"))
        slug = _optional_nonempty_string(entry.get("slug")) or name
        description = _optional_nonempty_string(entry.get("description"))
        docs_url = _optional_nonempty_string(entry.get("docs_url"))
        requires_env = entry.get("requires_env")
        requires_env_list = requires_env if isinstance(requires_env, list) else []

        if _looks_like_remote_source(raw_source):
            source = raw_source
            source_ref = raw_entry_ref
            source_subdir = raw_entry_subdir
        else:
            source = source_value
            source_ref = raw_entry_ref or ref_value
            source_subdir = raw_entry_subdir or raw_source

        resolved_docs_url = docs_url or _derive_github_readme_url(
            source=source,
            source_ref=source_ref,
            source_subdir=source_subdir,
        )

        resolved[name] = CatalogRecipeSpec(
            name=name,
            slug=slug,
            source=source,
            source_ref=source_ref,
            source_subdir=source_subdir,
            description=description,
            trusted=entry.get("trusted") if isinstance(entry.get("trusted"), bool) else None,
            docs_url=resolved_docs_url,
            requires_env=[str(item) for item in requires_env_list],
        )
    return resolved


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


def discover_recipe_entries(recipes_dir: Path) -> list[RecipeEntry]:
    """List recipe folders with metadata and enablement state."""
    if not recipes_dir.exists() or not recipes_dir.is_dir():
        return []

    manifest = load_manifest(recipes_dir)
    entries: list[RecipeEntry] = []
    seen_slugs: set[str] = set()
    for recipe_dir in sorted(path for path in recipes_dir.iterdir() if path.is_dir()):
        slug, recipe_error = _load_recipe_slug(recipe_dir)
        plugin, plugin_error = _load_plugin(recipe_dir)
        error = plugin_error or recipe_error
        manifest_record = get_manifest_record(manifest, slug)
        entries.append(
            RecipeEntry(
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

    recipes = manifest.get("recipes", {})
    if isinstance(recipes, dict):
        for slug, record in sorted(recipes.items()):
            if slug in seen_slugs or not isinstance(record, dict):
                continue
            folder = str(record.get("folder") or slug)
            orphan_path = recipes_dir / folder
            entries.append(
                RecipeEntry(
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


def find_recipe_entry(entries: list[RecipeEntry], slug_or_folder: str) -> RecipeEntry | None:
    """Locate recipe entry by slug or folder name."""
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
    """Build install commands from recipe metadata."""
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
        return "# No recipe dependency install steps."
    rendered = ["# Add these lines to your Dockerfile for recipe dependencies:"]
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


def metadata_status_payload(plugin: PluginConfig, *, app_version: str) -> dict[str, object]:
    """Build metadata payload with computed readiness status."""
    return build_plugin_payload(plugin, current_web2api_version=app_version)


def run_healthcheck(
    plugin: PluginConfig,
    *,
    timeout_seconds: float = 15.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run recipe metadata healthcheck command and return structured status."""
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
    """Resolve recipe source type from source value."""
    if Path(source).expanduser().exists():
        return "local"
    return "git"


@contextmanager
def checkout_source(
    source: str,
    *,
    source_ref: str | None = None,
    source_type: SourceType | None = None,
    sparse_paths: list[str] | None = None,
) -> Path:
    """Yield a local checkout path for a source value."""
    resolved_type = source_type or resolve_source_type(source)
    if resolved_type == "local":
        yield Path(source).expanduser().resolve()
        return

    normalized_sparse_paths: list[str] = []
    if sparse_paths is not None:
        normalized_sparse_paths = [
            path.strip()
            for path in sparse_paths
            if isinstance(path, str) and path.strip()
        ]

    with tempfile.TemporaryDirectory(prefix="web2api-recipe-src-") as tmp_dir:
        target = Path(tmp_dir) / "repo"
        if normalized_sparse_paths:
            try:
                subprocess.run(
                    ["git", "init", "--quiet", str(target)],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "remote", "add", "origin", source],
                    check=True,
                    text=True,
                )
                fetch_ref = source_ref or "HEAD"
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "fetch",
                        "--quiet",
                        "--depth",
                        "1",
                        "--filter=blob:none",
                        "origin",
                        fetch_ref,
                    ],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "sparse-checkout", "init", "--cone"],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "sparse-checkout",
                        "set",
                        *normalized_sparse_paths,
                    ],
                    check=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "checkout", "--quiet", "FETCH_HEAD"],
                    check=True,
                    text=True,
                )
            except subprocess.CalledProcessError:
                logger.info(
                    "Sparse checkout failed for %s; falling back to full clone.",
                    source,
                )
                if target.exists():
                    shutil.rmtree(target)
                clone_cmd = ["git", "clone", "--quiet", source, str(target)]
                subprocess.run(clone_cmd, check=True, text=True)
                if source_ref is not None:
                    subprocess.run(
                        ["git", "-C", str(target), "checkout", "--quiet", source_ref],
                        check=True,
                        text=True,
                    )
        else:
            clone_cmd = ["git", "clone", "--quiet", source, str(target)]
            subprocess.run(clone_cmd, check=True, text=True)
            if source_ref is not None:
                subprocess.run(
                    ["git", "-C", str(target), "checkout", "--quiet", source_ref],
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
    recipes_dir.mkdir(parents=True, exist_ok=True)
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


def install_recipe_from_source(
    *,
    source: str,
    recipes_dir: Path,
    source_ref: str | None = None,
    source_subdir: str | None = None,
    trusted: bool,
    overwrite: bool = False,
    record_source_type: SourceType | None = None,
    expected_slug: str | None = None,
) -> tuple[str, SourceType]:
    """Install a recipe from source path/git and persist manifest record."""
    resolved_source_type = resolve_source_type(source)
    manifest_source_type = record_source_type or resolved_source_type
    sparse_paths = (
        [source_subdir]
        if resolved_source_type == "git" and source_subdir
        else None
    )

    with checkout_source(
        source,
        source_ref=source_ref,
        source_type=resolved_source_type,
        sparse_paths=sparse_paths,
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

    record_recipe_install(
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
