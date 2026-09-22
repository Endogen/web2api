"""Tests for centralized application settings."""

from pathlib import Path

import pytest

from web2api.settings import AppSettings


def test_settings_use_stable_defaults_without_reading_process_env(tmp_path: Path) -> None:
    settings = AppSettings.from_env({}, default_recipes_dir=tmp_path / "recipes")

    assert settings.recipes_dir == tmp_path / "recipes"
    assert settings.catalog_path == "catalog.yaml"
    assert settings.browser_proxy is None
    assert settings.allow_private_network is False
    assert settings.pool_max_contexts == 5


def test_settings_parse_overrides_from_supplied_mapping(tmp_path: Path) -> None:
    settings = AppSettings.from_env(
        {
            "RECIPES_DIR": str(tmp_path / "custom"),
            "POOL_MAX_CONTEXTS": "3",
            "CACHE_ENABLED": "no",
            "WEB2API_ALLOW_PRIVATE_NETWORK": "yes",
            "WEB2API_BROWSER_PROXY": "http://proxy.example:8080",
            "WEB2API_MCP_ALLOWED_HOSTS": "api.example,localhost",
        }
    )

    assert settings.recipes_dir == tmp_path / "custom"
    assert settings.pool_max_contexts == 3
    assert settings.cache_enabled is False
    assert settings.allow_private_network is True
    assert settings.browser_proxy == "http://proxy.example:8080"
    assert settings.mcp_allowed_hosts == ("api.example", "localhost")


def test_settings_reject_invalid_numeric_limits() -> None:
    with pytest.raises(ValueError, match="POOL_MAX_CONTEXTS"):
        AppSettings.from_env({"POOL_MAX_CONTEXTS": "0"})

    with pytest.raises(ValueError, match="SCRAPE_TIMEOUT must be a number"):
        AppSettings.from_env({"SCRAPE_TIMEOUT": "soon"})
