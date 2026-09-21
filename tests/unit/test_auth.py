"""Unit tests for access-token authentication helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.datastructures import Headers

from web2api.auth import AuthConfig, load_auth_config, public_auth_payload, request_is_authorized


def test_auth_config_loads_token_from_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = tmp_path / "token.txt"
    secret_file.write_text("secret-token\n", encoding="utf-8")
    monkeypatch.delenv("WEB2API_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("WEB2API_ACCESS_TOKEN_FILE", str(secret_file))

    config = load_auth_config()

    assert config.enabled is True
    assert config.access_token == "secret-token"
    assert config.public_path_patterns == ("/", "/health")


def test_auth_config_loads_extra_public_paths_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB2API_ACCESS_TOKEN_FILE", raising=False)
    monkeypatch.setenv("WEB2API_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv(
        "WEB2API_PUBLIC_PATHS",
        "/api/sites, /allenai/*\n/docs,/openapi.json",
    )

    config = load_auth_config()

    assert config.public_path_patterns == (
        "/",
        "/health",
        "/api/sites",
        "/allenai/*",
        "/docs",
        "/openapi.json",
    )


def test_request_is_authorized_supports_bearer_and_alt_header() -> None:
    config = AuthConfig(access_token="secret-token")

    assert request_is_authorized(
        Headers({"authorization": "Bearer secret-token"}),
        config,
    ) is True
    assert request_is_authorized(
        Headers({"x-web2api-key": "secret-token"}),
        config,
    ) is True
    assert request_is_authorized(
        Headers({"authorization": "Bearer wrong"}),
        config,
    ) is False


def test_auth_config_requires_auth_for_all_non_public_routes() -> None:
    config = AuthConfig(access_token="secret-token")

    assert config.requires_auth("/") is False
    assert config.requires_auth("/health") is False
    assert config.requires_auth("/health/") is False
    assert config.requires_auth("/api/sites") is True
    assert config.requires_auth("/alpha/read") is True
    assert config.requires_auth("/mcp/tools") is True


def test_auth_config_matches_additional_public_path_patterns() -> None:
    config = AuthConfig(
        access_token="secret-token",
        public_path_patterns=("/", "/health", "/api/sites", "/allenai/*", "/*/chat"),
    )

    assert config.requires_auth("/api/sites") is False
    assert config.requires_auth("/allenai/chat") is False
    assert config.requires_auth("/foo/chat") is False
    assert config.requires_auth("/foo/read") is True


def test_auth_config_rejects_invalid_public_path_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB2API_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("WEB2API_PUBLIC_PATHS", "api/sites")

    with pytest.raises(ValueError, match="WEB2API_PUBLIC_PATHS entries must start with '/'"):
        load_auth_config()


def test_public_auth_payload_describes_public_and_protected_surfaces() -> None:
    payload = public_auth_payload(
        AuthConfig(
            access_token="secret-token",
            public_path_patterns=("/", "/health", "/api/sites"),
        )
    )

    assert payload["enabled"] is True
    assert payload["protected_surfaces"] == ["all routes except configured public path patterns"]
    assert payload["public_surfaces"] == ["/", "/health", "/api/sites"]
    assert payload["public_paths_env"] == "WEB2API_PUBLIC_PATHS"


def test_admin_api_fails_closed_without_any_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB2API_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEB2API_ACCESS_TOKEN_FILE", raising=False)
    monkeypatch.delenv("WEB2API_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("WEB2API_ALLOW_UNAUTHENTICATED_ADMIN", raising=False)

    config = load_auth_config()

    assert config.admin_is_disabled("/api/recipes/manage") is True
    assert config.requires_auth("/api/recipes/manage/install/demo") is True


def test_admin_token_is_distinct_from_general_access_token() -> None:
    config = AuthConfig(access_token="read-token", admin_token="admin-token")
    headers = Headers({"authorization": "Bearer read-token"})

    assert request_is_authorized(headers, config, path="/alpha/read") is True
    assert request_is_authorized(headers, config, path="/api/recipes/manage") is False
    assert request_is_authorized(
        Headers({"authorization": "Bearer admin-token"}),
        config,
        path="/api/recipes/manage",
    ) is True
