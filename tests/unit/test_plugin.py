"""Unit tests for plugin metadata and runtime checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from web2api.plugin import build_plugin_payload, parse_plugin_config


def _valid_plugin_data() -> dict[str, object]:
    return {
        "version": "1.0.0",
        "web2api": {"min": "0.2.0", "max": "1.0.0"},
        "requires_env": ["PLUGIN_TOKEN"],
        "dependencies": {
            "commands": ["bird"],
            "python": ["httpx"],
            "apt": ["nodejs"],
            "npm": ["@steipete/bird"],
        },
        "healthcheck": {"command": ["bird", "--version"]},
    }


def test_valid_plugin_config_parses() -> None:
    """A valid plugin definition should parse and preserve alias fields."""
    config = parse_plugin_config(_valid_plugin_data())

    assert config.version == "1.0.0"
    assert config.web2api.min_version == "0.2.0"
    assert config.web2api.max_version == "1.0.0"
    assert config.requires_env == ["PLUGIN_TOKEN"]
    assert config.dependencies.python_packages == ["httpx"]
    assert config.dependencies.apt_packages == ["nodejs"]
    assert config.dependencies.npm_packages == ["@steipete/bird"]
    assert config.healthcheck is not None
    assert config.healthcheck.command == ["bird", "--version"]


def test_invalid_required_env_name_raises_validation_error() -> None:
    """Env vars in requires_env must be uppercase identifier names."""
    data = _valid_plugin_data()
    data["requires_env"] = ["bad-name"]

    with pytest.raises(ValidationError):
        parse_plugin_config(data)


def test_invalid_version_bound_raises_validation_error() -> None:
    """Version bounds must use numeric major.minor.patch style."""
    data = _valid_plugin_data()
    data["web2api"] = {"min": "v0.2.0"}

    with pytest.raises(ValidationError):
        parse_plugin_config(data)


def test_plugin_payload_status_reports_missing_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dependency checks should report missing env vars/commands/packages."""
    config = parse_plugin_config(_valid_plugin_data())

    monkeypatch.setattr(
        "web2api.plugin.which",
        lambda command: "/usr/bin/bird" if command == "bird" else None,
    )
    monkeypatch.setattr(
        "web2api.plugin.util.find_spec",
        lambda package: object() if package == "httpx" else None,
    )

    payload = build_plugin_payload(
        config,
        environ={},
        current_web2api_version="0.3.0",
    )

    status = payload["status"]
    assert status["ready"] is False
    assert status["checks"]["env"]["missing"] == ["PLUGIN_TOKEN"]
    assert status["checks"]["commands"]["missing"] == []
    assert status["checks"]["python"]["missing"] == []
    assert status["compatibility"]["is_compatible"] is True
    assert status["unverified"]["apt"] == ["nodejs"]
    assert status["unverified"]["npm"] == ["@steipete/bird"]


def test_plugin_payload_status_marks_version_incompatible() -> None:
    """Compatibility bounds should fail readiness when out of range."""
    config = parse_plugin_config(_valid_plugin_data())
    payload = build_plugin_payload(
        config,
        environ={"PLUGIN_TOKEN": "set"},
        current_web2api_version="1.2.0",
    )

    status = payload["status"]
    assert status["ready"] is False
    assert status["compatibility"]["is_compatible"] is False
