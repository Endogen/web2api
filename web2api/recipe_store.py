"""Recipe manifest, trust, and filesystem discovery services."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from web2api.plugin import PluginConfig, build_plugin_payload
from web2api.recipe_loader import load_plugin_config, load_recipe_config

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
    """Atomically write recipe install-state manifest with private permissions."""
    recipes_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(recipes_dir)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=recipes_dir,
            prefix=f".{MANIFEST_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        directory_fd = os.open(recipes_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
    """Return an explicit trust flag; missing or malformed records fail closed."""
    if isinstance(entry_record, dict) and isinstance(entry_record.get("trusted"), bool):
        return bool(entry_record["trusted"])
    return False


def catalog_entry_is_trusted(trusted: bool | None) -> bool:
    """Resolve catalog trust with an explicit, safe default.

    A catalog entry without an explicit ``trusted`` flag is treated as
    untrusted (``False``). This mirrors the install-time behavior used by
    both the CLI and the admin API so the two cannot diverge.
    """
    return trusted is True


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


def resolve_recipe_path(recipes_dir: Path, folder: str) -> Path:
    """Resolve a recipe folder and require it to stay inside *recipes_dir*."""
    normalized = Path(folder)
    if not folder or normalized.name != folder or folder in {".", ".."}:
        raise ValueError(f"invalid recipe folder: {folder!r}")
    root = recipes_dir.expanduser().resolve()
    resolved = (root / normalized).resolve()
    if root not in resolved.parents:
        raise ValueError(f"recipe folder escapes recipes directory: {folder!r}")
    return resolved


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
    trusted = False
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
    installed_tree_hash: str | None = None,
) -> dict[str, Any]:
    """Upsert an installed recipe record in manifest."""
    manifest = load_manifest(recipes_dir)
    recipes = manifest["recipes"]
    assert isinstance(recipes, dict)

    record: dict[str, Any] = {
        "folder": folder,
        "source_type": source_type,
        "source": source,
        "source_ref": source_ref,
        "source_subdir": source_subdir,
        "trusted": trusted,
        "installed_at": datetime.now(UTC).isoformat(),
    }
    if installed_tree_hash is not None:
        record["installed_tree_hash"] = installed_tree_hash
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
    try:
        config = load_recipe_config(recipe_dir)
        return config.slug, None
    except Exception as exc:  # noqa: BLE001
        return recipe_dir.name, str(exc)


def _load_plugin(recipe_dir: Path) -> tuple[PluginConfig | None, str | None]:
    try:
        return load_plugin_config(recipe_dir), None
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
