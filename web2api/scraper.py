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


class InvalidParamsError(ValueError):
    """Raised by a scraper when request parameters are malformed.

    The scraping engine maps this to an ``INVALID_PARAMS`` error response
    instead of the generic ``SCRAPE_FAILED``.
    """


def coerce_int(value: Any, *, name: str, default: int) -> int:
    """Coerce a user-supplied parameter to ``int``, raising on bad input."""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParamsError(
            f"invalid {name} parameter: {value!r} (expected an integer)"
        ) from exc


def coerce_float(
    value: Any,
    *,
    name: str,
    default: float | None = None,
) -> float | None:
    """Coerce a user-supplied parameter to ``float``, raising on bad input."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParamsError(
            f"invalid {name} parameter: {value!r} (expected a number)"
        ) from exc


class BaseScraper(ABC):
    """Base class for optional recipe-specific scraper implementations.

    Subclasses override ``scrape()`` to handle one or more named endpoints.
    Browser scrapers receive a blank Playwright page. Direct HTTP or CLI
    scrapers set ``requires_browser = False`` and receive ``None`` instead.

    ``params`` contains ``page`` (int, 1-based page number) and ``query``
    (str | None).
    """

    requires_browser = True

    def supports(self, endpoint: str) -> bool:
        """Return ``True`` when this scraper handles *endpoint*.

        Override this to declare which endpoints use custom scraping
        instead of declarative YAML extraction.
        """
        return False

    async def scrape(
        self,
        endpoint: str,
        page: Page | None,
        params: dict[str, Any],
    ) -> ScrapeResult:
        """Scrape content for the given endpoint.

        Browser scrapers receive a blank, network-guarded page and must
        navigate it themselves. Direct scrapers receive ``None``.
        """
        raise NotImplementedError(f"endpoint '{endpoint}' is not implemented")
