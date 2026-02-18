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
        "capabilities": ["read"],
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
    assert config.capabilities == ["read"]
    assert config.endpoints.read is not None
    assert config.endpoints.search is None


def test_invalid_recipe_config_raises_validation_error() -> None:
    """Missing endpoint for a declared capability should fail validation."""
    data = _valid_recipe_data()
    data["capabilities"] = ["read", "search"]

    with pytest.raises(ValidationError):
        RecipeConfig.model_validate(data)


def test_config_defaults_are_applied() -> None:
    """Optional config fields should receive documented defaults."""
    config = RecipeConfig.model_validate(_valid_recipe_data())
    assert config.endpoints.read is not None

    title_field = config.endpoints.read.items.fields["title"]
    assert title_field.attribute == "text"
    assert title_field.context == "self"
    assert title_field.transform == "strip"
    assert title_field.optional is False
    assert config.endpoints.read.actions == []
    assert config.endpoints.read.pagination.start == 1
    assert config.endpoints.read.pagination.step == 1


def test_slug_matching_validation() -> None:
    """Slug must match the recipe folder name when requested."""
    data = _valid_recipe_data()
    config = RecipeConfig.model_validate(data)
    config.assert_slug_matches_folder("example")

    mismatch = deepcopy(data)
    mismatch["slug"] = "different"

    with pytest.raises(ValueError):
        parse_recipe_config(mismatch, folder_name="example")
