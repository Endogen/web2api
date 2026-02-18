"""Pydantic models for the unified API response schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ErrorCode = Literal[
    "SITE_NOT_FOUND",
    "CAPABILITY_NOT_SUPPORTED",
    "SCRAPE_FAILED",
    "SCRAPE_TIMEOUT",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
]
FieldValue = str | int | float | bool | None


class SiteInfo(BaseModel):
    """Metadata describing the scraped site."""

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    url: str


class ItemResponse(BaseModel):
    """Normalized representation of one scraped item."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    url: str | None = None
    fields: dict[str, FieldValue] = Field(default_factory=dict)


class PaginationResponse(BaseModel):
    """Pagination details returned with each response."""

    model_config = ConfigDict(extra="forbid")

    current_page: int = Field(ge=1)
    has_next: bool
    has_prev: bool
    total_pages: int | None = Field(default=None, ge=1)
    total_items: int | None = Field(default=None, ge=0)


class MetadataResponse(BaseModel):
    """Operational metadata for the scrape request."""

    model_config = ConfigDict(extra="forbid")

    scraped_at: datetime
    response_time_ms: int = Field(ge=0)
    item_count: int = Field(ge=0)
    cached: bool = False


class ErrorResponse(BaseModel):
    """Error payload for failed requests."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    details: str | None = None


class ApiResponse(BaseModel):
    """Top-level response returned by recipe endpoints."""

    model_config = ConfigDict(extra="forbid")

    site: SiteInfo
    endpoint: str
    query: str | None = None
    items: list[ItemResponse] = Field(default_factory=list)
    pagination: PaginationResponse
    metadata: MetadataResponse
    error: ErrorResponse | None = None
