"""Smoke tests for initial project bootstrap."""

import tomllib
from pathlib import Path

from web2api import __version__


def test_smoke() -> None:
    """Ensure pytest discovers and runs tests."""
    assert True


def test_package_version_matches_project_metadata() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert __version__ == project["project"]["version"]
