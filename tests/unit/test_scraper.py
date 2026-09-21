"""Unit tests for scraper base helpers (coercion + InvalidParamsError)."""

from __future__ import annotations

import pytest

from web2api.scraper import InvalidParamsError, coerce_float, coerce_int


def test_coerce_int_parses_values() -> None:
    assert coerce_int("20", name="count", default=10) == 20
    assert coerce_int(7, name="count", default=10) == 7


def test_coerce_int_uses_default_for_missing_values() -> None:
    assert coerce_int(None, name="count", default=10) == 10
    assert coerce_int("", name="count", default=10) == 10


def test_coerce_int_raises_invalid_params_on_bad_input() -> None:
    with pytest.raises(InvalidParamsError, match="count"):
        coerce_int("abc", name="count", default=10)


def test_coerce_float_parses_values() -> None:
    assert coerce_float("0.5", name="temperature") == 0.5
    assert coerce_float(2, name="temperature") == 2.0


def test_coerce_float_uses_default_for_missing_values() -> None:
    assert coerce_float(None, name="temperature") is None
    assert coerce_float("", name="temperature", default=1.0) == 1.0


def test_coerce_float_raises_invalid_params_on_bad_input() -> None:
    with pytest.raises(InvalidParamsError, match="temperature"):
        coerce_float("not-a-number", name="temperature")


def test_invalid_params_error_is_a_value_error() -> None:
    assert issubclass(InvalidParamsError, ValueError)
