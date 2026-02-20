"""Plugin metadata models and runtime dependency checks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from importlib import util
from shutil import which
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_NUMERIC_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){0,2}$")


def _normalize_deduplicated(values: list[str], *, label: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value:
            raise ValueError(f"{label} entries must not be empty")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _parse_numeric_version(value: str) -> tuple[int, int, int] | None:
    if not _NUMERIC_VERSION_PATTERN.match(value):
        return None
    parts = [int(part) for part in value.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _is_python_package_available(name: str) -> bool:
    candidates = (name, name.replace("-", "_"))
    for candidate in candidates:
        try:
            if util.find_spec(candidate) is not None:
                return True
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
    return False


class PluginCompatibility(BaseModel):
    """Optional Web2API version compatibility constraints."""

    model_config = ConfigDict(extra="forbid")

    min_version: str | None = Field(default=None, alias="min")
    max_version: str | None = Field(default=None, alias="max")

    @field_validator("min_version", "max_version")
    @classmethod
    def _validate_version_bound(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("version bounds must not be empty")
        if not _NUMERIC_VERSION_PATTERN.match(normalized):
            raise ValueError("version bounds must use numeric format (major.minor.patch)")
        return normalized


class PluginDependencies(BaseModel):
    """External dependencies required by a recipe plugin."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    commands: list[str] = Field(default_factory=list)
    python_packages: list[str] = Field(default_factory=list, alias="python")
    apt_packages: list[str] = Field(default_factory=list, alias="apt")
    npm_packages: list[str] = Field(default_factory=list, alias="npm")

    @field_validator("commands", "python_packages", "apt_packages", "npm_packages", mode="after")
    @classmethod
    def _normalize_lists(cls, value: list[str]) -> list[str]:
        return _normalize_deduplicated(value, label="dependency")


class PluginHealthcheck(BaseModel):
    """Optional command used by operators to validate plugin setup."""

    model_config = ConfigDict(extra="forbid")

    command: list[str]

    @field_validator("command", mode="after")
    @classmethod
    def _validate_command(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("healthcheck command must contain at least one token")
        command: list[str] = []
        for token in value:
            normalized = token.strip()
            if not normalized:
                raise ValueError("healthcheck command tokens must not be empty")
            command.append(normalized)
        return command


class PluginConfig(BaseModel):
    """Schema for optional ``plugin.yaml`` metadata."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: str
    web2api: PluginCompatibility = Field(default_factory=PluginCompatibility)
    requires_env: list[str] = Field(default_factory=list)
    dependencies: PluginDependencies = Field(default_factory=PluginDependencies)
    healthcheck: PluginHealthcheck | None = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("version must not be empty")
        return normalized

    @field_validator("requires_env", mode="after")
    @classmethod
    def _validate_required_env(cls, value: list[str]) -> list[str]:
        env_names = _normalize_deduplicated(value, label="requires_env")
        for env_name in env_names:
            if not _ENV_NAME_PATTERN.match(env_name):
                raise ValueError(
                    "requires_env entries must match [A-Z_][A-Z0-9_]* "
                    f"(invalid: {env_name!r})"
                )
        return env_names


def parse_plugin_config(data: Mapping[str, object]) -> PluginConfig:
    """Validate plugin config data from ``plugin.yaml``."""
    return PluginConfig.model_validate(data)


def _compatibility_status(
    compatibility: PluginCompatibility, current_web2api_version: str | None
) -> dict[str, Any]:
    min_version = compatibility.min_version
    max_version = compatibility.max_version
    is_compatible: bool | None = None

    if current_web2api_version is not None and (min_version is not None or max_version is not None):
        current_parts = _parse_numeric_version(current_web2api_version)
        min_parts = _parse_numeric_version(min_version) if min_version is not None else None
        max_parts = _parse_numeric_version(max_version) if max_version is not None else None
        if current_parts is not None and (min_parts is not None or min_version is None):
            if max_parts is not None or max_version is None:
                is_compatible = True
                if min_parts is not None and current_parts < min_parts:
                    is_compatible = False
                if max_parts is not None and current_parts > max_parts:
                    is_compatible = False

    return {
        "current": current_web2api_version,
        "min": min_version,
        "max": max_version,
        "is_compatible": is_compatible,
    }


def evaluate_plugin_status(
    plugin: PluginConfig,
    *,
    environ: Mapping[str, str] | None = None,
    current_web2api_version: str | None = None,
) -> dict[str, Any]:
    """Compute runtime readiness status for a plugin definition."""
    env = os.environ if environ is None else environ

    required_env = plugin.requires_env
    present_env = [name for name in required_env if env.get(name)]
    missing_env = [name for name in required_env if name not in present_env]

    required_commands = plugin.dependencies.commands
    present_commands = [name for name in required_commands if which(name) is not None]
    missing_commands = [name for name in required_commands if name not in present_commands]

    required_python = plugin.dependencies.python_packages
    present_python = [name for name in required_python if _is_python_package_available(name)]
    missing_python = [name for name in required_python if name not in present_python]

    compatibility = _compatibility_status(plugin.web2api, current_web2api_version)
    compatibility_ok = compatibility["is_compatible"] is not False

    ready = not missing_env and not missing_commands and not missing_python and compatibility_ok

    return {
        "ready": ready,
        "checks": {
            "env": {
                "required": required_env,
                "present": present_env,
                "missing": missing_env,
            },
            "commands": {
                "required": required_commands,
                "present": present_commands,
                "missing": missing_commands,
            },
            "python": {
                "required": required_python,
                "present": present_python,
                "missing": missing_python,
            },
        },
        "unverified": {
            "apt": plugin.dependencies.apt_packages,
            "npm": plugin.dependencies.npm_packages,
        },
        "compatibility": compatibility,
    }


def build_plugin_payload(
    plugin: PluginConfig,
    *,
    environ: Mapping[str, str] | None = None,
    current_web2api_version: str | None = None,
) -> dict[str, Any]:
    """Serialize plugin metadata with computed readiness status."""
    payload = {
        "version": plugin.version,
        "web2api": plugin.web2api.model_dump(by_alias=True),
        "requires_env": plugin.requires_env,
        "dependencies": plugin.dependencies.model_dump(by_alias=True),
        "healthcheck": None,
        "status": evaluate_plugin_status(
            plugin,
            environ=environ,
            current_web2api_version=current_web2api_version,
        ),
    }
    if plugin.healthcheck is not None:
        payload["healthcheck"] = plugin.healthcheck.model_dump()
    return payload
