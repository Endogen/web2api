"""Custom scraper interface for recipe-level Python overrides."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page


@dataclass(slots=True)
class ScrapeResult:
    """Normalized output returned by declarative or custom scraping."""

    items: list[dict[str, Any]] = field(default_factory=list)
    current_page: int = 1
    has_next: bool = False
    has_prev: bool = False
    total_pages: int | None = None
    total_items: int | None = None


class BaseScraper(ABC):
    """Base class for optional recipe-specific scraper implementations."""

    async def read(self, page: Page, params: dict[str, Any]) -> ScrapeResult:
        """Scrape content for a read endpoint."""
        raise NotImplementedError("read is not implemented")

    async def search(self, page: Page, params: dict[str, Any]) -> ScrapeResult:
        """Scrape content for a search endpoint."""
        raise NotImplementedError("search is not implemented")

    def supports_read(self) -> bool:
        """Return ``True`` when the subclass overrides ``read``."""
        return type(self).read is not BaseScraper.read

    def supports_search(self) -> bool:
        """Return ``True`` when the subclass overrides ``search``."""
        return type(self).search is not BaseScraper.search
