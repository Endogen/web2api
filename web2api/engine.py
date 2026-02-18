"""Scraping engine implementation for declarative and custom recipes."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import quote_plus, urljoin

from playwright.async_api import ElementHandle, Page

from web2api.config import ActionConfig, EndpointConfig, FieldConfig, ItemsConfig, PaginationConfig
from web2api.logging_utils import log_event
from web2api.pool import BrowserPool
from web2api.registry import Recipe
from web2api.schemas import (
    ApiResponse,
    ErrorCode,
    ErrorResponse,
    ItemResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)
from web2api.scraper import ScrapeResult

logger = logging.getLogger(__name__)


def build_url(endpoint: EndpointConfig, *, page: int, query: str | None = None) -> str:
    """Build a request URL by resolving recipe template placeholders."""
    current_page = max(page, 1)
    mapped_page = endpoint.pagination.start + (current_page - 1)
    page_zero = current_page - 1
    encoded_query = quote_plus(query or "")

    return (
        endpoint.url.replace("{page}", str(mapped_page))
        .replace("{page_zero}", str(page_zero))
        .replace("{query}", encoded_query)
    )


async def execute_actions(page: Page, actions: list[ActionConfig]) -> None:
    """Execute endpoint actions sequentially."""
    for index, action in enumerate(actions):
        try:
            if action.type == "wait":
                await page.wait_for_selector(action.selector, timeout=action.timeout)
            elif action.type == "click":
                await page.click(action.selector)
            elif action.type == "scroll":
                if action.amount == "bottom":
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                else:
                    delta = action.amount if action.direction == "down" else -action.amount
                    await page.evaluate("(pixels) => window.scrollBy(0, pixels)", delta)
            elif action.type == "type":
                await page.fill(action.selector, action.text)
            elif action.type == "sleep":
                await page.wait_for_timeout(action.ms)
            elif action.type == "evaluate":
                await page.evaluate(action.script)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"action {index} ({action.type}) failed: {exc}") from exc


async def extract_items(
    page: Page, items_config: ItemsConfig, *, base_url: str
) -> list[dict[str, Any]]:
    """Extract item dictionaries from the current page."""
    containers = await page.query_selector_all(items_config.container)
    items: list[dict[str, Any]] = []

    for container in containers:
        item: dict[str, Any] = {}
        for field_name, field_config in items_config.fields.items():
            item[field_name] = await _extract_field(container, field_config, base_url=base_url)
        items.append(item)
    return items


def apply_transform(value: Any, transform: str | None, *, base_url: str) -> Any:
    """Apply a field transform, returning ``None`` on transform failure."""
    if value is None:
        return None
    if transform is None:
        return value

    raw_text = str(value)
    if transform == "strip":
        try:
            return raw_text.strip()
        except Exception as exc:  # noqa: BLE001
            _log_transform_failure(transform=transform, value=raw_text, error=exc)
            return None
    if transform == "strip_html":
        try:
            return re.sub(r"<[^>]+>", "", raw_text).strip()
        except Exception as exc:  # noqa: BLE001
            _log_transform_failure(transform=transform, value=raw_text, error=exc)
            return None
    if transform == "regex_int":
        try:
            match = re.search(r"-?\d+", raw_text)
            if match is None:
                _log_transform_failure(
                    transform=transform,
                    value=raw_text,
                    reason="no_match",
                )
                return None
            return int(match.group(0))
        except Exception as exc:  # noqa: BLE001
            _log_transform_failure(transform=transform, value=raw_text, error=exc)
            return None
    if transform == "regex_float":
        try:
            match = re.search(r"-?\d+(?:\.\d+)?", raw_text)
            if match is None:
                _log_transform_failure(
                    transform=transform,
                    value=raw_text,
                    reason="no_match",
                )
                return None
            return float(match.group(0))
        except Exception as exc:  # noqa: BLE001
            _log_transform_failure(transform=transform, value=raw_text, error=exc)
            return None
    if transform == "iso_date":
        try:
            transformed = _to_iso_date(raw_text)
            if transformed is None:
                _log_transform_failure(
                    transform=transform,
                    value=raw_text,
                    reason="unparseable_date",
                )
            return transformed
        except Exception as exc:  # noqa: BLE001
            _log_transform_failure(transform=transform, value=raw_text, error=exc)
            return None
    if transform == "absolute_url":
        try:
            return urljoin(base_url, raw_text)
        except Exception as exc:  # noqa: BLE001
            _log_transform_failure(transform=transform, value=raw_text, error=exc)
            return None
    raise ValueError(f"unknown transform '{transform}'")


def _log_transform_failure(
    *,
    transform: str,
    value: str,
    reason: str | None = None,
    error: Exception | None = None,
) -> None:
    preview = value if len(value) <= 120 else f"{value[:117]}..."
    log_event(
        logger,
        logging.WARNING,
        "transform.failed",
        transform=transform,
        reason=reason,
        value_preview=preview,
        error=str(error) if error is not None else None,
    )


async def detect_pagination(
    page: Page,
    pagination: PaginationConfig,
    *,
    current_page: int,
    item_count: int,
) -> tuple[bool, bool, int | None, int | None]:
    """Detect pagination values after extraction."""
    has_prev = current_page > 1
    if pagination.type in {"page_param", "offset_param"}:
        return item_count > 0, has_prev, None, None

    next_link = await page.query_selector(pagination.selector)
    return next_link is not None, has_prev, None, None


async def scrape(
    *,
    pool: BrowserPool,
    recipe: Recipe,
    endpoint: str,
    page: int = 1,
    query: str | None = None,
    scrape_timeout: float = 30.0,
) -> ApiResponse:
    """Run a scrape and return a unified API response."""
    started_at = perf_counter()
    current_page = max(page, 1)
    log_event(
        logger,
        logging.INFO,
        "scrape.started",
        site_slug=recipe.config.slug,
        endpoint=endpoint,
        page=current_page,
        has_query=bool(query),
    )

    endpoint_config = recipe.config.endpoints.get(endpoint)
    if endpoint_config is None:
        return _error_response(
            recipe=recipe,
            endpoint=endpoint,
            query=query,
            current_page=current_page,
            started_at=started_at,
            code="CAPABILITY_NOT_SUPPORTED",
            message=f"endpoint '{endpoint}' is not defined for recipe '{recipe.config.slug}'",
        )

    if endpoint_config.requires_query and not query:
        return _error_response(
            recipe=recipe,
            endpoint=endpoint,
            query=query,
            current_page=current_page,
            started_at=started_at,
            code="INVALID_PARAMS",
            message=f"missing required query parameter 'q' for endpoint '{endpoint}'",
        )

    used_custom_scraper = False

    async def _do_scrape() -> ScrapeResult:
        nonlocal used_custom_scraper
        async with pool.page() as browser_page:
            custom_result = await _run_custom_scraper(
                recipe=recipe,
                endpoint=endpoint,
                page=browser_page,
                current_page=current_page,
                query=query,
            )
            if custom_result is not None:
                used_custom_scraper = True
                return custom_result

            url = build_url(endpoint_config, page=current_page, query=query)
            await browser_page.goto(url)
            await execute_actions(browser_page, endpoint_config.actions)
            raw_items = await extract_items(
                browser_page,
                endpoint_config.items,
                base_url=recipe.config.base_url,
            )
            has_next, has_prev, total_pages, total_items = await detect_pagination(
                browser_page,
                endpoint_config.pagination,
                current_page=current_page,
                item_count=len(raw_items),
            )
            return ScrapeResult(
                items=raw_items,
                current_page=current_page,
                has_next=has_next,
                has_prev=has_prev,
                total_pages=total_pages,
                total_items=total_items,
            )

    try:
        result = await asyncio.wait_for(_do_scrape(), timeout=scrape_timeout)
    except TimeoutError:
        return _error_response(
            recipe=recipe,
            endpoint=endpoint,
            query=query,
            current_page=current_page,
            started_at=started_at,
            code="SCRAPE_TIMEOUT",
            message=f"scrape exceeded {scrape_timeout}s timeout",
        )
    except Exception as exc:  # noqa: BLE001
        return _error_response(
            recipe=recipe,
            endpoint=endpoint,
            query=query,
            current_page=current_page,
            started_at=started_at,
            code="SCRAPE_FAILED",
            message=str(exc),
        )

    items = _normalize_items(result.items)
    elapsed_ms = int((perf_counter() - started_at) * 1000)
    log_event(
        logger,
        logging.INFO,
        "scrape.completed",
        site_slug=recipe.config.slug,
        endpoint=endpoint,
        page=result.current_page,
        response_time_ms=elapsed_ms,
        item_count=len(items),
        custom_scraper=used_custom_scraper,
    )
    return ApiResponse(
        site=SiteInfo(
            name=recipe.config.name,
            slug=recipe.config.slug,
            url=recipe.config.base_url,
        ),
        endpoint=endpoint,
        query=query if endpoint_config.requires_query else None,
        items=items,
        pagination=PaginationResponse(
            current_page=result.current_page,
            has_next=result.has_next,
            has_prev=result.has_prev,
            total_pages=result.total_pages,
            total_items=result.total_items,
        ),
        metadata=MetadataResponse(
            scraped_at=datetime.now(UTC),
            response_time_ms=elapsed_ms,
            item_count=len(items),
            cached=False,
        ),
        error=None,
    )


async def _extract_field(
    container: ElementHandle,
    field_config: FieldConfig,
    *,
    base_url: str,
) -> Any:
    root = await _resolve_context_root(container, field_config.context)
    if root is None:
        if field_config.optional:
            return None
        raise RuntimeError(f"context '{field_config.context}' could not be resolved")

    if field_config.selector == "":
        element = root
    else:
        element = await root.query_selector(field_config.selector)
    if element is None:
        if field_config.optional:
            return None
        raise RuntimeError(f"required field selector not found: {field_config.selector}")

    value = await _get_attribute_value(element, field_config.attribute)
    return apply_transform(value, field_config.transform, base_url=base_url)


async def _resolve_context_root(container: ElementHandle, context: str) -> ElementHandle | None:
    if context == "self":
        return container

    if context == "next_sibling":
        handle = await container.evaluate_handle("el => el.nextElementSibling")
    elif context == "parent":
        handle = await container.evaluate_handle("el => el.parentElement")
    else:
        raise RuntimeError(f"unknown field context '{context}'")

    return handle.as_element()


async def _get_attribute_value(element: ElementHandle, attribute: str) -> str | None:
    if attribute == "text":
        return await element.text_content()
    return await element.get_attribute(attribute)


async def _run_custom_scraper(
    *,
    recipe: Recipe,
    endpoint: str,
    page: Page,
    current_page: int,
    query: str | None,
) -> ScrapeResult | None:
    """Invoke a recipe's custom scraper if one is registered.

    The ``page`` passed to the scraper is a **blank** Playwright page with no
    URL loaded.  The custom scraper is responsible for navigating to the target
    URL.  If no custom scraper exists or the scraper does not support the
    requested endpoint, ``None`` is returned and the engine falls back to
    declarative YAML extraction.
    """
    if recipe.scraper is None:
        return None

    if not recipe.scraper.supports(endpoint):
        return None

    params = {"page": current_page, "query": query}
    return await recipe.scraper.scrape(endpoint, page, params)


def _normalize_items(raw_items: list[dict[str, Any]]) -> list[ItemResponse]:
    items: list[ItemResponse] = []
    for raw_item in raw_items:
        fields = {key: value for key, value in raw_item.items() if key not in {"title", "url"}}
        title = raw_item.get("title")
        url = raw_item.get("url")
        items.append(
            ItemResponse(
                title=str(title) if title is not None else None,
                url=str(url) if url is not None else None,
                fields=fields,
            )
        )
    return items


def _to_iso_date(value: str) -> str | None:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    with_timezone = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(with_timezone).date().isoformat()
    except ValueError:
        return None


def _error_response(
    *,
    recipe: Recipe,
    endpoint: str,
    query: str | None,
    current_page: int,
    started_at: float,
    code: ErrorCode,
    message: str,
) -> ApiResponse:
    elapsed_ms = int((perf_counter() - started_at) * 1000)
    level = logging.ERROR if code in {"SCRAPE_FAILED", "INTERNAL_ERROR"} else logging.WARNING
    log_event(
        logger,
        level,
        "scrape.failed",
        site_slug=recipe.config.slug,
        endpoint=endpoint,
        page=current_page,
        response_time_ms=elapsed_ms,
        error_code=code,
        error_message=message,
    )
    return ApiResponse(
        site=SiteInfo(
            name=recipe.config.name,
            slug=recipe.config.slug,
            url=recipe.config.base_url,
        ),
        endpoint=endpoint,
        query=query,
        items=[],
        pagination=PaginationResponse(
            current_page=current_page,
            has_next=False,
            has_prev=current_page > 1,
            total_pages=None,
            total_items=None,
        ),
        metadata=MetadataResponse(
            scraped_at=datetime.now(UTC),
            response_time_ms=elapsed_ms,
            item_count=0,
            cached=False,
        ),
        error=ErrorResponse(
            code=code,
            message=message,
            details=None,
        ),
    )
