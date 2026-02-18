"""Unit tests for recipe configuration models."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from web2api.config import RecipeConfig, parse_recipe_config


def _valid_recipe_data() -> dict[str, object]:
    return {
        "name": "Example Site",
        "slug": "example",
        "base_url": "https://example.com",
        "description": "Example recipe used by unit tests.",
        "endpoints": {
            "read": {
                "url": "https://example.com/items?page={page}",
                "items": {
                    "container": ".item",
                    "fields": {
                        "title": {"selector": ".title"},
                        "url": {"selector": ".title-link", "attribute": "href"},
                    },
                },
                "pagination": {
                    "type": "page_param",
                    "param": "page",
                },
            }
        },
    }


def test_valid_recipe_config_parses() -> None:
    """Valid recipe data should parse into a RecipeConfig model."""
    config = RecipeConfig.model_validate(_valid_recipe_data())

    assert config.slug == "example"
    assert "read" in config.endpoints
    assert config.endpoint_names == ["read"]


def test_empty_endpoints_raises_validation_error() -> None:
    """No endpoints at all should fail validation."""
    data = _valid_recipe_data()
    data["endpoints"] = {}

    with pytest.raises(ValidationError):
        RecipeConfig.model_validate(data)


def test_invalid_endpoint_name_raises_validation_error() -> None:
    """Endpoint names with invalid characters should fail."""
    data = _valid_recipe_data()
    data["endpoints"] = {
        "INVALID": {
            "url": "https://example.com/items?page={page}",
            "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
            "pagination": {"type": "page_param", "param": "page"},
        }
    }

    with pytest.raises(ValidationError):
        RecipeConfig.model_validate(data)


def test_config_defaults_are_applied() -> None:
    """Optional config fields should receive documented defaults."""
    config = RecipeConfig.model_validate(_valid_recipe_data())
    read_endpoint = config.endpoints["read"]

    title_field = read_endpoint.items.fields["title"]
    assert title_field.attribute == "text"
    assert title_field.context == "self"
    assert title_field.transform == "strip"
    assert title_field.optional is False
    assert read_endpoint.actions == []
    assert read_endpoint.pagination.start == 1
    assert read_endpoint.pagination.step == 1
    assert read_endpoint.requires_query is False
    assert read_endpoint.description is None


def test_requires_query_field() -> None:
    """Endpoint with requires_query should parse correctly."""
    data = _valid_recipe_data()
    data["endpoints"]["search"] = {
        "url": "https://example.com/search?q={query}&page={page}",
        "description": "Search items",
        "requires_query": True,
        "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
        "pagination": {"type": "page_param", "param": "page"},
    }
    config = RecipeConfig.model_validate(data)
    assert config.endpoints["search"].requires_query is True
    assert config.endpoints["search"].description == "Search items"
    assert sorted(config.endpoint_names) == ["read", "search"]


def test_multiple_custom_endpoints() -> None:
    """Recipe with arbitrary named endpoints should parse."""
    data = {
        "name": "Translator",
        "slug": "translator",
        "base_url": "https://example.com",
        "description": "Translation recipe",
        "endpoints": {
            "de-en": {
                "url": "https://example.com/translate/de/en",
                "description": "German to English",
                "requires_query": True,
                "items": {"container": ".result", "fields": {"text": {"selector": ".text"}}},
                "pagination": {"type": "page_param", "param": "p"},
            },
            "en-de": {
                "url": "https://example.com/translate/en/de",
                "description": "English to German",
                "requires_query": True,
                "items": {"container": ".result", "fields": {"text": {"selector": ".text"}}},
                "pagination": {"type": "page_param", "param": "p"},
            },
        },
    }
    config = RecipeConfig.model_validate(data)
    assert config.endpoint_names == ["de-en", "en-de"]
    assert config.endpoints["de-en"].requires_query is True


def test_slug_matching_validation() -> None:
    """Slug must match the recipe folder name when requested."""
    data = _valid_recipe_data()
    config = RecipeConfig.model_validate(data)
    config.assert_slug_matches_folder("example")

    mismatch = deepcopy(data)
    mismatch["slug"] = "different"

    with pytest.raises(ValueError):
        parse_recipe_config(mismatch, folder_name="example")
