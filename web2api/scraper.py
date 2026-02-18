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
    """Base class for optional recipe-specific scraper implementations.

    Subclasses override ``scrape()`` to handle one or more named endpoints.
    The ``page`` is a **blank** Playwright page — no URL has been loaded.
    The scraper must navigate to the target URL itself.

    ``params`` contains ``page`` (int, 1-based page number) and ``query``
    (str | None).
    """

    def supports(self, endpoint: str) -> bool:
        """Return ``True`` when this scraper handles *endpoint*.

        Override this to declare which endpoints use custom scraping
        instead of declarative YAML extraction.
        """
        return False

    async def scrape(self, endpoint: str, page: Page, params: dict[str, Any]) -> ScrapeResult:
        """Scrape content for the given endpoint.

        The ``page`` is a **blank** Playwright page — no URL has been loaded.
        The scraper must navigate to the target URL itself (e.g. via
        ``await page.goto(...)``).
        """
        raise NotImplementedError(f"endpoint '{endpoint}' is not implemented")
