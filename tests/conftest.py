"""Test configuration shared across unit/integration suites."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _allow_test_admin_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy admin-route fixtures open unless a test opts into auth."""
    monkeypatch.setenv("WEB2API_ALLOW_UNAUTHENTICATED_ADMIN", "true")
