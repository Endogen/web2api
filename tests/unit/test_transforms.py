"""Unit tests for scraping field transforms."""

from __future__ import annotations

import logging

import pytest

from web2api.engine import apply_transform


def test_regex_int_transform() -> None:
    assert apply_transform("153 points", "regex_int", base_url="https://example.com") == 153
    assert apply_transform("no match", "regex_int", base_url="https://example.com") is None


def test_regex_float_transform() -> None:
    assert apply_transform("$19.99", "regex_float", base_url="https://example.com") == 19.99


def test_strip_transform() -> None:
    assert apply_transform("  hello  ", "strip", base_url="https://example.com") == "hello"


def test_strip_html_transform() -> None:
    assert apply_transform("<b>hello</b>", "strip_html", base_url="https://example.com") == "hello"


def test_absolute_url_transform() -> None:
    assert (
        apply_transform("/page", "absolute_url", base_url="https://example.com")
        == "https://example.com/page"
    )


def test_iso_date_transform() -> None:
    assert (
        apply_transform("Jan 5, 2026", "iso_date", base_url="https://example.com") == "2026-01-05"
    )
    assert apply_transform("2026/02/18", "iso_date", base_url="https://example.com") == "2026-02-18"


def test_unknown_transform_raises_error() -> None:
    with pytest.raises(ValueError):
        apply_transform("value", "unknown_transform", base_url="https://example.com")


def test_transform_failure_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        value = apply_transform("no digits here", "regex_int", base_url="https://example.com")

    assert value is None
    assert any(
        getattr(record, "event", None) == "transform.failed"
        and getattr(record, "transform", None) == "regex_int"
        for record in caplog.records
    )
