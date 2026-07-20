"""Configuration models for declarative recipe definitions."""

from __future__ import annotations

import keyword
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActionType = Literal["wait", "click", "scroll", "type", "sleep", "evaluate"]
PaginationType = Literal["page_param", "next_link", "offset_param"]
FieldContext = Literal["self", "next_sibling", "parent"]
RESERVED_RECIPE_SLUGS = {"api", "health", "docs", "openapi", "redoc"}
_RESERVED_PARAM_NAMES = {"q", "page"}
FieldTransform = Literal[
    "regex_int",
    "regex_float",
    "strip",
    "strip_html",
    "iso_date",
    "absolute_url",
]


def is_valid_param_name(name: str) -> bool:
    """Return ``True`` if *name* is a valid Python identifier and not a keyword."""
    return name.isidentifier() and not keyword.iskeyword(name)


class ActionConfig(BaseModel):
    """Playwright action executed before extraction."""

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    selector: str | None = None
    timeout: int | None = Field(default=None, ge=0)
    direction: Literal["down", "up"] | None = None
    amount: int | Literal["bottom"] | None = Field(default=None, ge=0)
    text: str | None = None
    ms: int | None = Field(default=None, ge=0)
    script: str | None = None

    @model_validator(mode="after")
    def validate_required_fields(self) -> ActionConfig:
        """Validate action fields required by action type."""
        required: dict[ActionType, tuple[str, ...]] = {
            "wait": ("selector",),
            "click": ("selector",),
            "scroll": ("direction", "amount"),
            "type": ("selector", "text"),
            "sleep": ("ms",),
            "evaluate": ("script",),
        }
        missing = [field for field in required[self.type] if getattr(self, field) in (None, "")]
        if missing:
            missing_list = ", ".join(missing)
            raise ValueError(f"action '{self.type}' is missing required field(s): {missing_list}")
        return self


class FieldConfig(BaseModel):
    """Definition for extracting one item field."""

    model_config = ConfigDict(extra="forbid")

    selector: str
    attribute: str = "text"
    context: FieldContext = "self"
    transform: FieldTransform | None = "strip"
    optional: bool = False


class ItemsConfig(BaseModel):
    """Extraction settings for repeated items on a page."""

    model_config = ConfigDict(extra="forbid")

    container: str
    fields: dict[str, FieldConfig]

    @model_validator(mode="after")
    def validate_has_fields(self) -> ItemsConfig:
        """Require at least one field mapping."""
        if not self.fields:
            raise ValueError("items.fields must define at least one field")
        return self


class PaginationConfig(BaseModel):
    """Pagination strategy for an endpoint."""

    model_config = ConfigDict(extra="forbid")

    type: PaginationType
    param: str | None = None
    selector: str | None = None
    start: int = Field(default=1, ge=0)
    step: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def validate_pagination_fields(self) -> PaginationConfig:
        """Validate pagination fields required by pagination type."""
        if self.type in {"page_param", "offset_param"} and not self.param:
            raise ValueError(f"pagination type '{self.type}' requires 'param'")
        if self.type == "next_link" and not self.selector:
            raise ValueError("pagination type 'next_link' requires 'selector'")
        return self


class ParamConfig(BaseModel):
    """Typed declaration of an extra query parameter for an endpoint."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    required: bool = False
    type: Literal["string", "integer", "number", "boolean"] = "string"
    example: Any = None
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    pattern: str | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_constraints(self) -> ParamConfig:
        """Ensure constraints match the declared value type."""
        def _matches_type(value: Any) -> bool:
            if self.type == "string":
                return isinstance(value, str)
            if self.type == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            if self.type == "number":
                return isinstance(value, (int, float)) and not isinstance(value, bool)
            return isinstance(value, bool)

        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("minimum must be less than or equal to maximum")
        if self.min_length is not None and self.max_length is not None:
            if self.min_length > self.max_length:
                raise ValueError("min_length must be less than or equal to max_length")
        if self.type != "string" and any(
            value is not None for value in (self.pattern, self.min_length, self.max_length)
        ):
            raise ValueError("pattern and length constraints require type 'string'")
        if self.type not in {"integer", "number"} and any(
            value is not None for value in (self.minimum, self.maximum)
        ):
            raise ValueError("minimum and maximum require a numeric type")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern: {exc}") from exc
        if self.example is not None and not _matches_type(self.example):
            raise ValueError(f"example must match declared type '{self.type}'")
        if self.enum is not None:
            if not self.enum:
                raise ValueError("enum must contain at least one value")
            if any(not _matches_type(value) for value in self.enum):
                raise ValueError(f"enum values must match declared type '{self.type}'")
        return self


class EndpointConfig(BaseModel):
    """Recipe endpoint configuration."""

    model_config = ConfigDict(extra="forbid")

    url: str
    description: str | None = None
    requires_query: bool = False
    tool_name: str | None = None
    actions: list[ActionConfig] = Field(default_factory=list)
    items: ItemsConfig
    pagination: PaginationConfig
    params: dict[str, ParamConfig] = Field(default_factory=dict)
    accepts_files: bool = False

    @model_validator(mode="after")
    def validate_params(self) -> EndpointConfig:
        """Validate that param names are valid and don't conflict with reserved names."""
        for name in self.params:
            if name in _RESERVED_PARAM_NAMES:
                raise ValueError(
                    f"param name '{name}' conflicts with reserved query parameter"
                )
            if not is_valid_param_name(name):
                raise ValueError(
                    f"param name '{name}' must be a valid Python identifier "
                    "(letters/digits/underscore, not starting with a digit, not a keyword)"
                )
        return self


class RecipeConfig(BaseModel):
    """Top-level recipe configuration loaded from ``recipe.yaml``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    base_url: str
    description: str
    endpoints: dict[str, EndpointConfig]

    @model_validator(mode="after")
    def validate_endpoints(self) -> RecipeConfig:
        """Ensure at least one endpoint is defined and names are valid."""
        if not self.endpoints:
            raise ValueError("at least one endpoint must be defined")
        if self.slug in RESERVED_RECIPE_SLUGS:
            reserved = ", ".join(sorted(RESERVED_RECIPE_SLUGS))
            raise ValueError(f"recipe slug '{self.slug}' is reserved ({reserved})")
        name_pattern = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
        for name in self.endpoints:
            if not name_pattern.match(name):
                raise ValueError(
                    f"endpoint name '{name}' must match pattern [a-z0-9][a-z0-9_-]*"
                )
        return self

    @property
    def endpoint_names(self) -> list[str]:
        """Return sorted list of endpoint names."""
        return sorted(self.endpoints.keys())

    def assert_slug_matches_folder(self, folder_name: str) -> None:
        """Raise if the recipe slug does not match the folder name."""
        if self.slug != folder_name:
            raise ValueError(
                f"recipe slug '{self.slug}' does not match folder name '{folder_name}'"
            )


def parse_recipe_config(data: Mapping[str, object], folder_name: str | None = None) -> RecipeConfig:
    """Validate recipe config data and optionally enforce folder/slug matching."""
    config = RecipeConfig.model_validate(data)
    if folder_name is not None:
        config.assert_slug_matches_folder(folder_name)
    return config
