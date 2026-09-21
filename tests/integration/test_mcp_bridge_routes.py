"""Integration tests for MCP HTTP bridge routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from web2api.main import create_app
from web2api.schemas import (
    ApiResponse,
    ErrorCode,
    ErrorResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)


class FakePool:
    """Pool stub used for MCP bridge route tests."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def health(self) -> dict[str, int | bool]:
        return {
            "browser_connected": True,
            "total_contexts": 1,
            "available_contexts": 1,
            "queue_size": 0,
            "total_requests_served": 0,
        }


def _write_recipe(recipes_dir: Path, slug: str) -> None:
    recipe_dir = recipes_dir / slug
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "recipe.yaml").write_text(
        yaml.safe_dump(
            {
                "name": slug.title(),
                "slug": slug,
                "base_url": "https://example.com",
                "description": f"{slug} fixture recipe",
                "endpoints": {
                    "read": {
                        "url": "https://example.com/items?page={page}",
                        "items": {
                            "container": ".item",
                            "fields": {"title": {"selector": ".title"}},
                        },
                        "pagination": {"type": "page_param", "param": "page"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _success_response(*, slug: str, endpoint: str, page: int) -> ApiResponse:
    return ApiResponse(
        site=SiteInfo(name=slug.title(), slug=slug, url="https://example.com"),
        endpoint=endpoint,
        query=None,
        items=[],
        pagination=PaginationResponse(
            current_page=page,
            has_next=False,
            has_prev=page > 1,
            total_pages=None,
            total_items=None,
        ),
        metadata=MetadataResponse(
            scraped_at=datetime.now(UTC),
            response_time_ms=1,
            item_count=0,
            cached=False,
        ),
        error=None,
    )


def _error_response(*, slug: str, endpoint: str, page: int, code: ErrorCode) -> ApiResponse:
    return ApiResponse(
        site=SiteInfo(name=slug.title(), slug=slug, url="https://example.com"),
        endpoint=endpoint,
        query=None,
        items=[],
        pagination=PaginationResponse(
            current_page=page,
            has_next=False,
            has_prev=page > 1,
            total_pages=None,
            total_items=None,
        ),
        metadata=MetadataResponse(
            scraped_at=datetime.now(UTC),
            response_time_ms=1,
            item_count=0,
            cached=False,
        ),
        error=ErrorResponse(code=code, message="boom", details=None),
    )


@pytest.mark.asyncio
async def test_mcp_call_tool_honors_page_param(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "demo")

    seen_pages: list[int] = []

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: str,
        page: int = 1,
        query: str | None = None,
        extra_params: dict[str, str] | None = None,
        scrape_timeout: float = 30.0,
        direct_semaphore: asyncio.Semaphore | None = None,
        allow_private_network: bool = False,
    ) -> ApiResponse:
        _ = pool, query, extra_params, scrape_timeout, direct_semaphore, allow_private_network
        seen_pages.append(page)
        return _success_response(slug=recipe.config.slug, endpoint=endpoint, page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    app = create_app(recipes_dir=recipes_dir, pool=FakePool())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/mcp/tools/demo_read", json={"page": 3})

    assert resp.status_code == 200
    assert seen_pages == [3]


@pytest.mark.asyncio
async def test_mcp_call_tool_error_uses_mapped_status_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "demo")

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: str,
        page: int = 1,
        query: str | None = None,
        extra_params: dict[str, str] | None = None,
        scrape_timeout: float = 30.0,
        direct_semaphore: asyncio.Semaphore | None = None,
        allow_private_network: bool = False,
    ) -> ApiResponse:
        _ = pool, query, extra_params, scrape_timeout, direct_semaphore, allow_private_network
        return _error_response(
            slug=recipe.config.slug,
            endpoint=endpoint,
            page=page,
            code="INVALID_PARAMS",
        )

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    app = create_app(recipes_dir=recipes_dir, pool=FakePool())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/mcp/tools/demo_read", json={"q": "x"})

    assert resp.status_code == 400
    assert resp.json()["result"] == "Error: boom"


@pytest.mark.asyncio
async def test_mcp_call_tool_filtered_enforces_exclude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "demo")

    called = False

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: str,
        page: int = 1,
        query: str | None = None,
        extra_params: dict[str, str] | None = None,
        scrape_timeout: float = 30.0,
        direct_semaphore: asyncio.Semaphore | None = None,
        allow_private_network: bool = False,
    ) -> ApiResponse:
        _ = pool, query, extra_params, scrape_timeout, direct_semaphore, allow_private_network
        nonlocal called
        called = True
        return _success_response(slug=recipe.config.slug, endpoint=endpoint, page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    app = create_app(recipes_dir=recipes_dir, pool=FakePool())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/mcp/exclude/demo/tools/demo_read",
                json={"q": "x"},
            )

    assert resp.status_code == 404
    assert called is False


@pytest.mark.asyncio
async def test_mcp_call_tool_filtered_enforces_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "demo")

    seen_pages: list[int] = []

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: str,
        page: int = 1,
        query: str | None = None,
        extra_params: dict[str, str] | None = None,
        scrape_timeout: float = 30.0,
        direct_semaphore: asyncio.Semaphore | None = None,
        allow_private_network: bool = False,
    ) -> ApiResponse:
        _ = pool, query, extra_params, scrape_timeout, direct_semaphore, allow_private_network
        seen_pages.append(page)
        return _success_response(slug=recipe.config.slug, endpoint=endpoint, page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    app = create_app(recipes_dir=recipes_dir, pool=FakePool())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            blocked = await client.post(
                "/mcp/only/other/tools/demo_read",
                json={"q": "x"},
            )
            allowed = await client.post(
                "/mcp/only/demo/tools/demo_read",
                json={"page": 2},
            )

    assert blocked.status_code == 404
    assert allowed.status_code == 200
    assert seen_pages == [2]
