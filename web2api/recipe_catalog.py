"""Recipe catalog loading and source resolution services."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from web2api.recipe_install import checkout_source, resolve_source_type
from web2api.recipe_store import (
    CATALOG_ENV_NAME_PATTERN,
    CatalogRecipeSpec,
    default_catalog_path,
    default_catalog_ref,
    default_catalog_source,
)


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
                f"{catalog_file} recipe '{recipe_name}' field "
                "'requires_env' must not contain empty entries"
            )
        if not CATALOG_ENV_NAME_PATTERN.match(env_name):
            raise ValueError(
                f"{catalog_file} recipe '{recipe_name}' field "
                f"'requires_env' has invalid env name {env_name!r}"
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
    encoded_subdir = "/".join(
        quote(part, safe="")
        for part in cleaned_subdir.split("/")
        if part and part != "."
    )
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
