"""Shared helpers for Web2API and plugin version comparisons."""

from __future__ import annotations

import re

_NUMERIC_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){0,2}$")


def is_numeric_version(value: str) -> bool:
    """Return whether *value* is a supported numeric version bound."""
    return _NUMERIC_VERSION_PATTERN.fullmatch(value) is not None


def parse_numeric_version(value: str) -> tuple[int, int, int] | None:
    """Parse one-to-three numeric version components into a comparable tuple."""
    if not is_numeric_version(value):
        return None
    parts = [int(part) for part in value.split(".")]
    parts.extend([0] * (3 - len(parts)))
    return tuple(parts)
