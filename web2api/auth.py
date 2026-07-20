"""Authentication helpers for protected Web2API HTTP surfaces."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

ACCESS_TOKEN_ENV = "WEB2API_ACCESS_TOKEN"
ACCESS_TOKEN_FILE_ENV = "WEB2API_ACCESS_TOKEN_FILE"
PUBLIC_PATHS_ENV = "WEB2API_PUBLIC_PATHS"
ADMIN_TOKEN_ENV = "WEB2API_ADMIN_TOKEN"
ALLOW_UNAUTHENTICATED_ADMIN_ENV = "WEB2API_ALLOW_UNAUTHENTICATED_ADMIN"
AUTH_HEADER = "Authorization"
ALT_AUTH_HEADER = "X-Web2API-Key"
AUTH_STORAGE_KEY = "web2api.access_token"
BASE_PUBLIC_PATHS = ("/", "/health")
ADMIN_PATH_PREFIX = "/api/recipes/manage"


@dataclass(slots=True)
class AuthConfig:
    """Runtime authentication configuration for protected HTTP routes."""

    access_token: str | None = None
    admin_token: str | None = None
    allow_unauthenticated_admin: bool = False
    public_path_patterns: tuple[str, ...] = BASE_PUBLIC_PATHS

    @property
    def enabled(self) -> bool:
        return self.access_token is not None

    def requires_auth(self, path: str) -> bool:
        """Return ``True`` when *path* is protected by access-token auth."""
        if self.is_admin_path(path):
            if self.admin_token is not None or self.access_token is not None:
                return True
            return not self.allow_unauthenticated_admin
        if not self.enabled:
            return False
        normalized_path = _normalize_path(path)
        return not any(
            _path_matches_pattern(normalized_path, pattern)
            for pattern in self.public_path_patterns
        )

    def is_admin_path(self, path: str) -> bool:
        """Return whether *path* belongs to the recipe-management API."""
        normalized = _normalize_path(path)
        return normalized == ADMIN_PATH_PREFIX or normalized.startswith(
            f"{ADMIN_PATH_PREFIX}/"
        )

    def admin_is_disabled(self, path: str) -> bool:
        """Return whether unauthenticated admin access is intentionally disabled."""
        return (
            self.is_admin_path(path)
            and self.admin_token is None
            and self.access_token is None
            and not self.allow_unauthenticated_admin
        )

    def token_for_path(self, path: str | None = None) -> str | None:
        """Return the token required for a request path."""
        if path is not None and self.is_admin_path(path) and self.admin_token is not None:
            return self.admin_token
        return self.access_token


def _normalize_path(path: str) -> str:
    if path == "":
        return "/"
    if path != "/" and path.endswith("/"):
        return path.rstrip("/") or "/"
    return path


def _normalize_public_path_pattern(pattern: str) -> str:
    normalized = pattern.strip()
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        raise ValueError(
            f"{PUBLIC_PATHS_ENV} entries must start with '/': {pattern!r}"
        )
    return _normalize_path(normalized)


def _load_public_path_patterns(raw_value: str | None) -> tuple[str, ...]:
    patterns: list[str] = list(BASE_PUBLIC_PATHS)
    if raw_value is None:
        return tuple(patterns)

    for chunk in raw_value.replace("\n", ",").split(","):
        normalized = _normalize_public_path_pattern(chunk)
        if normalized and normalized not in patterns:
            patterns.append(normalized)
    return tuple(patterns)


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if any(char in pattern for char in "*?[]"):
        return fnmatchcase(path, pattern)
    return path == pattern


def _read_secret_file(path_value: str) -> str | None:
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_file():
        raise ValueError(f"{ACCESS_TOKEN_FILE_ENV} does not point to a readable file: {path}")
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def load_auth_config() -> AuthConfig:
    """Load auth configuration from environment variables."""
    file_value = os.environ.get(ACCESS_TOKEN_FILE_ENV)
    public_path_patterns = _load_public_path_patterns(os.environ.get(PUBLIC_PATHS_ENV))
    token_value: str | None = None
    if file_value is not None and file_value.strip():
        token_value = _read_secret_file(file_value.strip())
    elif ACCESS_TOKEN_ENV in os.environ:
        raw_value = os.environ.get(ACCESS_TOKEN_ENV)
        if raw_value is not None:
            token_value = raw_value.strip() or None
    admin_value = os.environ.get(ADMIN_TOKEN_ENV, "").strip() or None
    allow_admin = os.environ.get(ALLOW_UNAUTHENTICATED_ADMIN_ENV, "").strip().lower()
    return AuthConfig(
        access_token=token_value,
        admin_token=admin_value,
        allow_unauthenticated_admin=allow_admin in {"1", "true", "yes", "on"},
        public_path_patterns=public_path_patterns,
    )


def _extract_bearer_token(header_value: str | None) -> str | None:
    if header_value is None:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    normalized = value.strip()
    return normalized or None


def request_is_authorized(
    headers: Any,
    config: AuthConfig,
    *,
    path: str | None = None,
) -> bool:
    """Return ``True`` when request headers satisfy configured auth."""
    required_token = config.token_for_path(path)
    if required_token is None:
        if path is not None and config.admin_is_disabled(path):
            return False
        return True

    provided = _extract_bearer_token(headers.get("authorization"))
    if provided is None:
        alternate = headers.get(ALT_AUTH_HEADER)
        if isinstance(alternate, str):
            provided = alternate.strip() or None

    if provided is None:
        return False
    return secrets.compare_digest(provided, required_token)


def public_auth_payload(config: AuthConfig) -> dict[str, Any]:
    """Serialize non-secret auth state for the index UI."""
    return {
        "enabled": config.enabled,
        "admin_enabled": config.admin_token is not None,
        "admin_locked": config.admin_is_disabled(ADMIN_PATH_PREFIX),
        "header": AUTH_HEADER,
        "scheme": "Bearer",
        "alternate_header": ALT_AUTH_HEADER,
        "storage_key": AUTH_STORAGE_KEY,
        "protected_surfaces": [
            "all routes except configured public path patterns",
        ],
        "public_surfaces": list(config.public_path_patterns),
        "public_paths_env": PUBLIC_PATHS_ENV,
    }
