"""Unit tests for unified schema helpers."""

from __future__ import annotations

import pytest

from web2api.schemas import ErrorResponse, status_code_for_error


def _error(code: str) -> ErrorResponse:
    return ErrorResponse(code=code, message="boom", details=None)


def test_status_code_for_error_returns_200_when_no_error() -> None:
    assert status_code_for_error(None) == 200


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("SITE_NOT_FOUND", 404),
        ("CAPABILITY_NOT_SUPPORTED", 400),
        ("INVALID_PARAMS", 400),
        ("SCRAPE_FAILED", 502),
        ("SCRAPE_TIMEOUT", 504),
        ("INTERNAL_ERROR", 500),
    ],
)
def test_status_code_mapping(code: str, expected: int) -> None:
    assert status_code_for_error(_error(code)) == expected


def test_status_code_defaults_to_500_for_unknown_code() -> None:
    error = ErrorResponse.model_construct(code="UNKNOWN_CODE", message="boom", details=None)
    assert status_code_for_error(error) == 500
