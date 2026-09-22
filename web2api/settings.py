"""Typed runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from web2api.auth import AuthConfig, load_auth_config

_TRUE_VALUES = {"1", "true", "yes", "on"}


def env_bool(
    name: str,
    *,
    default: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Read a conventional boolean environment variable."""
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    environ: Mapping[str, str],
) -> int:
    raw = environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(
    name: str,
    *,
    default: float,
    minimum: float,
    environ: Mapping[str, str],
) -> float:
    raw = environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_csv(name: str, *, default: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = environ.get(name, default)
    return tuple(value.strip() for value in raw.split(",") if value.strip())


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Validated application settings used by the app factory."""

    log_level: str
    recipes_dir: Path
    pool_max_contexts: int
    pool_acquire_timeout: float
    pool_page_timeout_ms: int
    pool_queue_size: int
    browser_proxy: str | None
    scrape_timeout: float
    direct_max_concurrency: int
    cache_enabled: bool
    cache_ttl_seconds: float
    cache_stale_ttl_seconds: float
    cache_max_entries: int
    allow_private_network: bool
    max_upload_files: int
    max_upload_bytes: int
    enforce_plugin_compatibility: bool
    catalog_source: str
    catalog_ref: str | None
    catalog_path: str
    mcp_allowed_hosts: tuple[str, ...]
    mcp_allowed_origins: tuple[str, ...]
    auth: AuthConfig

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        default_recipes_dir: Path | None = None,
    ) -> AppSettings:
        """Build settings from *environ* and validate numeric limits."""
        values = os.environ if environ is None else environ
        fallback_recipes = default_recipes_dir or Path.home() / ".web2api" / "recipes"
        recipes_dir = Path(values.get("RECIPES_DIR", str(fallback_recipes))).expanduser()
        catalog_ref = values.get("WEB2API_RECIPE_CATALOG_REF", "").strip() or None

        return cls(
            log_level=values.get("LOG_LEVEL", "info").strip().upper() or "INFO",
            recipes_dir=recipes_dir,
            pool_max_contexts=_env_int(
                "POOL_MAX_CONTEXTS", default=5, minimum=1, environ=values
            ),
            pool_acquire_timeout=_env_float(
                "POOL_ACQUIRE_TIMEOUT", default=30.0, minimum=0.001, environ=values
            ),
            pool_page_timeout_ms=_env_int(
                "POOL_PAGE_TIMEOUT", default=15_000, minimum=1, environ=values
            ),
            pool_queue_size=_env_int(
                "POOL_QUEUE_SIZE", default=20, minimum=0, environ=values
            ),
            browser_proxy=values.get("WEB2API_BROWSER_PROXY", "").strip() or None,
            scrape_timeout=_env_float(
                "SCRAPE_TIMEOUT", default=30.0, minimum=0.001, environ=values
            ),
            direct_max_concurrency=_env_int(
                "DIRECT_MAX_CONCURRENCY", default=20, minimum=1, environ=values
            ),
            cache_enabled=env_bool("CACHE_ENABLED", default=True, environ=values),
            cache_ttl_seconds=_env_float(
                "CACHE_TTL_SECONDS", default=30.0, minimum=0.0, environ=values
            ),
            cache_stale_ttl_seconds=_env_float(
                "CACHE_STALE_TTL_SECONDS", default=120.0, minimum=0.0, environ=values
            ),
            cache_max_entries=_env_int(
                "CACHE_MAX_ENTRIES", default=500, minimum=1, environ=values
            ),
            allow_private_network=env_bool(
                "WEB2API_ALLOW_PRIVATE_NETWORK", default=False, environ=values
            ),
            max_upload_files=_env_int(
                "WEB2API_MAX_UPLOAD_FILES", default=4, minimum=1, environ=values
            ),
            max_upload_bytes=_env_int(
                "WEB2API_MAX_UPLOAD_BYTES",
                default=25 * 1024 * 1024,
                minimum=1,
                environ=values,
            ),
            enforce_plugin_compatibility=env_bool(
                "PLUGIN_ENFORCE_COMPATIBILITY", default=False, environ=values
            ),
            catalog_source=values.get(
                "WEB2API_RECIPE_CATALOG_SOURCE",
                "https://github.com/Endogen/web2api-recipes.git",
            ).strip(),
            catalog_ref=catalog_ref,
            catalog_path=values.get("WEB2API_RECIPE_CATALOG_PATH", "catalog.yaml").strip()
            or "catalog.yaml",
            mcp_allowed_hosts=_env_csv(
                "WEB2API_MCP_ALLOWED_HOSTS",
                default=(
                    "127.0.0.1,127.0.0.1:*,localhost,localhost:*,"
                    "[::1],[::1]:*,testserver"
                ),
                environ=values,
            ),
            mcp_allowed_origins=_env_csv(
                "WEB2API_MCP_ALLOWED_ORIGINS",
                default=(
                    "http://127.0.0.1,http://127.0.0.1:*,"
                    "http://localhost,http://localhost:*"
                ),
                environ=values,
            ),
            auth=load_auth_config(values),
        )
