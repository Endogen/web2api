"""Integration-style tests for the scraping engine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from web2api.config import RecipeConfig
from web2api.engine import scrape
from web2api.registry import Recipe


class FakeJSHandle:
    """Stub for Playwright JS handles used by evaluate_handle."""

    def __init__(self, element: FakeElement | None) -> None:
        self._element = element

    def as_element(self) -> FakeElement | None:
        return self._element

    async def dispose(self) -> None:
        return None


class FakeElement:
    """Minimal element handle implementation for extraction tests."""

    def __init__(
        self,
        *,
        text: str | None = None,
        attributes: dict[str, str] | None = None,
        selector_map: dict[str, FakeElement] | None = None,
    ) -> None:
        self._text = text
        self._attributes = attributes or {}
        self._selector_map = selector_map or {}
        self.next_sibling: FakeElement | None = None
        self.parent: FakeElement | None = None

    async def query_selector(self, selector: str) -> FakeElement | None:
        return self._selector_map.get(selector)

    async def evaluate_handle(self, script: str) -> FakeJSHandle:
        if "nextElementSibling" in script:
            return FakeJSHandle(self.next_sibling)
        if "parentElement" in script:
            return FakeJSHandle(self.parent)
        return FakeJSHandle(None)

    async def text_content(self) -> str | None:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name)


class FakePage:
    """Minimal page implementation for scrape() integration tests."""

    def __init__(self) -> None:
        self.selector_all_map: dict[str, list[FakeElement]] = {}
        self.selector_map: dict[str, FakeElement] = {}
        self.goto_calls: list[str] = []
        self.action_log: list[tuple[str, Any]] = []
        self.fail_wait_selector = False

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    async def query_selector_all(self, selector: str) -> list[FakeElement]:
        return self.selector_all_map.get(selector, [])

    async def query_selector(self, selector: str) -> FakeElement | None:
        return self.selector_map.get(selector)

    async def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        if self.fail_wait_selector:
            raise RuntimeError("wait failed")
        self.action_log.append(("wait", selector, timeout))

    async def click(self, selector: str) -> None:
        self.action_log.append(("click", selector))

    async def fill(self, selector: str, text: str) -> None:
        self.action_log.append(("type", selector, text))

    async def wait_for_timeout(self, ms: int) -> None:
        self.action_log.append(("sleep", ms))

    async def evaluate(self, script: str, arg: Any | None = None) -> None:
        self.action_log.append(("evaluate", script, arg))


class FakePool:
    """Minimal pool exposing the async page() context manager."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    @asynccontextmanager
    async def page(self, timeout: float | None = None) -> AsyncIterator[FakePage]:
        _ = timeout
        yield self._page


def _build_recipe(endpoints: dict[str, Any]) -> Recipe:
    config = RecipeConfig.model_validate(
        {
            "name": "Example",
            "slug": "example",
            "base_url": "https://example.com",
            "description": "Fixture recipe",
            "endpoints": endpoints,
        }
    )
    return Recipe(config=config, scraper=None, path=Path("recipes/example"))


def _container_with_metadata(
    *,
    title: str,
    href: str,
    score: str | None,
    category: str,
) -> FakeElement:
    title_element = FakeElement(text=title, attributes={"href": href})
    container = FakeElement(selector_map={".title": title_element, ".title-link": title_element})
    score_selector = {".score": FakeElement(text=score)} if score is not None else {}
    metadata_row = FakeElement(selector_map=score_selector)
    parent = FakeElement(selector_map={".category": FakeElement(text=category)})
    container.next_sibling = metadata_row
    container.parent = parent
    return container


@pytest.mark.asyncio
async def test_scrape_extracts_items_with_context_and_transforms() -> None:
    endpoint = {
        "url": "https://example.com/list?page={page}",
        "items": {
            "container": ".item",
            "fields": {
                "title": {"selector": ".title", "attribute": "text"},
                "url": {"selector": ".title-link", "attribute": "href"},
                "score": {
                    "selector": ".score",
                    "context": "next_sibling",
                    "attribute": "text",
                    "transform": "regex_int",
                    "optional": True,
                },
                "category": {
                    "selector": ".category",
                    "context": "parent",
                    "attribute": "text",
                },
            },
        },
        "pagination": {"type": "next_link", "selector": ".next"},
    }
    recipe = _build_recipe({"read": endpoint})

    page = FakePage()
    page.selector_all_map[".item"] = [
        _container_with_metadata(
            title="Item 1",
            href="/item-1",
            score="153 points",
            category="news",
        ),
        _container_with_metadata(
            title="Item 2",
            href="/item-2",
            score="7 points",
            category="show",
        ),
    ]
    page.selector_map[".next"] = FakeElement()

    response = await scrape(pool=FakePool(page), recipe=recipe, endpoint="read", page=1)

    assert response.error is None
    assert response.metadata.item_count == 2
    assert response.pagination.has_next is True
    assert response.items[0].title == "Item 1"
    assert response.items[0].url == "/item-1"
    assert response.items[0].fields["score"] == 153
    assert response.items[0].fields["category"] == "news"


@pytest.mark.asyncio
async def test_scrape_handles_optional_fields() -> None:
    endpoint = {
        "url": "https://example.com/list?page={page}",
        "items": {
            "container": ".item",
            "fields": {
                "title": {"selector": ".title", "attribute": "text"},
                "score": {
                    "selector": ".score",
                    "context": "next_sibling",
                    "attribute": "text",
                    "transform": "regex_int",
                    "optional": True,
                },
            },
        },
        "pagination": {"type": "page_param", "param": "page"},
    }
    recipe = _build_recipe({"read": endpoint})

    page = FakePage()
    page.selector_all_map[".item"] = [
        _container_with_metadata(
            title="Item 1",
            href="/item-1",
            score=None,
            category="news",
        )
    ]

    response = await scrape(pool=FakePool(page), recipe=recipe, endpoint="read", page=1)

    assert response.error is None
    assert response.items[0].fields["score"] is None


@pytest.mark.asyncio
async def test_scrape_executes_actions() -> None:
    endpoint = {
        "url": "https://example.com/list?page={page}",
        "actions": [
            {"type": "wait", "selector": ".ready", "timeout": 1000},
            {"type": "click", "selector": ".next"},
            {"type": "type", "selector": "input[name=q]", "text": "python"},
            {"type": "sleep", "ms": 10},
            {"type": "evaluate", "script": "window.test = true"},
            {"type": "scroll", "direction": "down", "amount": 200},
        ],
        "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
        "pagination": {"type": "page_param", "param": "page"},
    }
    recipe = _build_recipe({"read": endpoint})
    page = FakePage()

    response = await scrape(pool=FakePool(page), recipe=recipe, endpoint="read", page=1)

    assert response.error is None
    assert page.action_log[:5] == [
        ("wait", ".ready", 1000),
        ("click", ".next"),
        ("type", "input[name=q]", "python"),
        ("sleep", 10),
        ("evaluate", "window.test = true", None),
    ]
    assert page.action_log[5][0] == "evaluate"
    assert page.action_log[5][2] == 200


@pytest.mark.asyncio
async def test_scrape_returns_error_when_action_fails() -> None:
    endpoint = {
        "url": "https://example.com/list?page={page}",
        "actions": [{"type": "wait", "selector": ".ready", "timeout": 1000}],
        "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
        "pagination": {"type": "page_param", "param": "page"},
    }
    recipe = _build_recipe({"read": endpoint})
    page = FakePage()
    page.fail_wait_selector = True

    response = await scrape(pool=FakePool(page), recipe=recipe, endpoint="read", page=1)

    assert response.error is not None
    assert response.error.code == "SCRAPE_FAILED"


@pytest.mark.asyncio
async def test_scrape_returns_error_for_unknown_endpoint() -> None:
    endpoint = {
        "url": "https://example.com/list?page={page}",
        "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
        "pagination": {"type": "page_param", "param": "page"},
    }
    recipe = _build_recipe({"read": endpoint})
    page = FakePage()

    response = await scrape(pool=FakePool(page), recipe=recipe, endpoint="nonexistent", page=1)

    assert response.error is not None
    assert response.error.code == "CAPABILITY_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_scrape_requires_query_validation() -> None:
    endpoint = {
        "url": "https://example.com/search?q={query}&page={page}",
        "requires_query": True,
        "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
        "pagination": {"type": "page_param", "param": "page"},
    }
    recipe = _build_recipe({"search": endpoint})
    page = FakePage()

    response = await scrape(pool=FakePool(page), recipe=recipe, endpoint="search", page=1)

    assert response.error is not None
    assert response.error.code == "INVALID_PARAMS"
