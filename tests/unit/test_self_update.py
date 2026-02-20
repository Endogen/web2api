"""Unit tests for self-update helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from web2api.self_update import (
    build_update_commands,
    check_for_updates,
    detect_update_method,
    resolve_latest_git_tag,
)


def test_detect_update_method_prefers_git_when_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "web2api.self_update.which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    assert detect_update_method(tmp_path) == "git"


def test_detect_update_method_uses_docker_when_compose_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "web2api.self_update.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    assert detect_update_method(tmp_path) == "docker"


def test_build_update_commands_for_pip_and_git() -> None:
    pip_commands = build_update_commands(method="pip", to_version="0.3.0")
    assert pip_commands[0][-1] == "web2api==0.3.0"

    git_commands = build_update_commands(method="git", to_version="v0.3.0")
    assert git_commands == [
        ["git", "fetch", "--tags", "--prune"],
        ["git", "checkout", "v0.3.0"],
    ]


def test_build_update_commands_rejects_docker_target() -> None:
    with pytest.raises(ValueError):
        _ = build_update_commands(method="docker", to_version="0.3.0")


def test_build_update_commands_rejects_git_without_target() -> None:
    with pytest.raises(ValueError):
        _ = build_update_commands(method="git", to_version=None)


def test_resolve_latest_git_tag_prefers_semver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "web2api.self_update.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode=0,
            stdout="notes\nv0.2.0\nv0.1.0\n",
            stderr="",
        ),
    )
    assert resolve_latest_git_tag() == "v0.2.0"


def test_resolve_latest_git_tag_returns_none_on_no_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "web2api.self_update.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )
    assert resolve_latest_git_tag() is None


def test_check_for_updates_sets_unknown_when_latest_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "web2api.self_update.fetch_latest_pypi_version",
        lambda package_name="web2api": None,
    )
    monkeypatch.setattr("web2api.self_update.detect_update_method", lambda cwd=None: "pip")

    result = check_for_updates(current_version="0.2.0", method="auto")
    assert result.latest_version is None
    assert result.update_available is None
    assert result.latest_git_tag is None


def test_check_for_updates_includes_latest_git_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "web2api.self_update.fetch_latest_pypi_version",
        lambda package_name="web2api": "0.3.0",
    )
    monkeypatch.setattr("web2api.self_update.resolve_latest_git_tag", lambda cwd=None: "v0.3.0")

    result = check_for_updates(current_version="0.2.0", method="git")
    assert result.latest_git_tag == "v0.3.0"
    assert result.update_available is True
