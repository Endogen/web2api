"""Self-update detection and command planning for Web2API CLI."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Literal

UpdateMethod = Literal["auto", "pip", "git", "docker"]
ResolvedUpdateMethod = Literal["pip", "git", "docker"]
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){0,2}$")
_SEMVER_TAG_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(slots=True)
class UpdateCheck:
    """Result of self-update check command."""

    current_version: str
    latest_version: str | None
    method: ResolvedUpdateMethod
    update_available: bool | None
    latest_git_tag: str | None = None


def _parse_numeric_version(value: str) -> tuple[int, int, int] | None:
    if not _VERSION_PATTERN.match(value):
        return None
    parts = [int(part) for part in value.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def detect_update_method(cwd: Path | None = None) -> ResolvedUpdateMethod:
    """Select best update method for current deployment shape."""
    location = cwd or Path.cwd()
    if which("git") and (location / ".git").exists():
        return "git"
    if which("docker") and any(
        (location / name).exists() for name in ("docker-compose.yml", "compose.yml")
    ):
        return "docker"
    return "pip"


def fetch_latest_pypi_version(
    package_name: str = "web2api",
    timeout_seconds: float = 4.0,
) -> str | None:
    """Fetch latest package version from PyPI JSON API."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None

    info = payload.get("info")
    if not isinstance(info, dict):
        return None

    latest = info.get("version")
    if not isinstance(latest, str):
        return None

    latest_version = latest.strip()
    return latest_version or None


def check_for_updates(
    *,
    current_version: str,
    method: UpdateMethod = "auto",
    cwd: Path | None = None,
) -> UpdateCheck:
    """Build a self-update check response."""
    resolved_method = detect_update_method(cwd) if method == "auto" else method
    if resolved_method not in {"pip", "git", "docker"}:
        raise ValueError(f"unsupported update method: {resolved_method}")

    latest_version = fetch_latest_pypi_version("web2api")
    latest_git_tag = resolve_latest_git_tag(cwd) if resolved_method == "git" else None
    update_available: bool | None = None
    if latest_version is not None:
        current_parts = _parse_numeric_version(current_version)
        latest_parts = _parse_numeric_version(latest_version)
        if current_parts is not None and latest_parts is not None:
            update_available = latest_parts > current_parts

    return UpdateCheck(
        current_version=current_version,
        latest_version=latest_version,
        method=resolved_method,
        update_available=update_available,
        latest_git_tag=latest_git_tag,
    )


def resolve_latest_git_tag(cwd: Path | None = None) -> str | None:
    """Return the highest sorted git tag, preferring semantic-version tags."""
    location = cwd or Path.cwd()
    result = subprocess.run(
        ["git", "-C", str(location), "tag", "--sort=-v:refname"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not tags:
        return None

    semver_tags = [tag for tag in tags if _SEMVER_TAG_PATTERN.match(tag)]
    if semver_tags:
        return semver_tags[0]
    return tags[0]


def build_update_commands(
    *,
    method: ResolvedUpdateMethod,
    to_version: str | None = None,
) -> list[list[str]]:
    """Plan shell commands required for selected update method."""
    if method == "pip":
        target = "web2api" if to_version is None else f"web2api=={to_version}"
        return [[sys.executable, "-m", "pip", "install", "--upgrade", target]]

    if method == "git":
        if to_version is None:
            raise ValueError("git update requires a tag or ref")
        return [
            ["git", "fetch", "--tags", "--prune"],
            ["git", "checkout", to_version],
        ]

    if to_version is not None:
        raise ValueError("docker update method does not support --to")
    return [
        ["docker", "compose", "pull"],
        ["docker", "compose", "up", "-d", "--build"],
    ]


def apply_update_commands(
    commands: list[list[str]],
    *,
    dry_run: bool = False,
) -> None:
    """Execute self-update commands."""
    for command in commands:
        if dry_run:
            continue
        subprocess.run(command, check=True, text=True)
