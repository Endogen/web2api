"""Integration tests for API routes and index endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from web2api.main import create_app
from web2api.schemas import (
    ApiResponse,
    EndpointType,
    ErrorCode,
    ErrorResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)


class FakePool:
    """Pool stub used for API integration tests."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    @property
    def health(self) -> dict[str, int | bool]:
        return {
            "browser_connected": True,
            "total_contexts": 1,
            "available_contexts": 1,
            "queue_size": 0,
            "total_requests_served": 0,
        }


def _write_recipe(recipes_dir: Path, slug: str, capabilities: list[str]) -> None:
    endpoints: dict[str, object] = {
        "read": {
            "url": "https://example.com/items?page={page}",
            "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
            "pagination": {"type": "page_param", "param": "page"},
        }
    }
    if "search" in capabilities:
        endpoints["search"] = {
            "url": "https://example.com/search?q={query}&page={page}",
            "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
            "pagination": {"type": "page_param", "param": "page"},
        }

    recipe_dir = recipes_dir / slug
    recipe_dir.mkdir(parents=True, exist_ok=True)
    (recipe_dir / "recipe.yaml").write_text(
        yaml.safe_dump(
            {
                "name": slug.title(),
                "slug": slug,
                "base_url": "https://example.com",
                "description": f"{slug} fixture recipe",
                "capabilities": capabilities,
                "endpoints": endpoints,
            }
        ),
        encoding="utf-8",
    )


def _success_response(
    *,
    slug: str,
    endpoint: EndpointType,
    page: int,
    query: str | None = None,
) -> ApiResponse:
    return ApiResponse(
        site=SiteInfo(name=slug.title(), slug=slug, url="https://example.com"),
        endpoint=endpoint,
        query=query if endpoint == "search" else None,
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


def _error_response(
    *,
    slug: str,
    endpoint: EndpointType,
    page: int,
    code: ErrorCode,
    message: str,
) -> ApiResponse:
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
        error=ErrorResponse(
            code=code,
            message=message,
            details=None,
        ),
    )


@pytest.mark.asyncio
async def test_api_routes_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "alpha", ["read", "search"])
    _write_recipe(recipes_dir, "beta", ["read"])

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: EndpointType,
        page: int = 1,
        query: str | None = None,
        scrape_timeout: float = 30.0,
    ) -> ApiResponse:
        _ = pool
        if endpoint not in recipe.config.capabilities:
            return _error_response(
                slug=recipe.config.slug,
                endpoint=endpoint,
                page=page,
                code="CAPABILITY_NOT_SUPPORTED",
                message="unsupported endpoint",
            )
        if endpoint == "search" and not query:
            return _error_response(
                slug=recipe.config.slug,
                endpoint=endpoint,
                page=page,
                code="INVALID_PARAMS",
                message="missing q",
            )
        return _success_response(
            slug=recipe.config.slug,
            endpoint=endpoint,
            page=page,
            query=query,
        )

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with caplog.at_level(logging.INFO):
                read_resp = await client.get(
                    "/alpha/read?page=2",
                    headers={"x-request-id": "req-alpha-read"},
                )
                assert read_resp.status_code == 200
                assert read_resp.json()["endpoint"] == "read"
                assert read_resp.json()["pagination"]["current_page"] == 2
                assert read_resp.headers["x-request-id"] == "req-alpha-read"

                search_resp = await client.get("/alpha/search?q=test&page=1")
                assert search_resp.status_code == 200
                assert search_resp.json()["endpoint"] == "search"
                assert search_resp.json()["query"] == "test"
                assert search_resp.headers["x-request-id"] != ""

                unsupported_resp = await client.get("/beta/search?q=test")
                assert unsupported_resp.status_code == 400
                assert unsupported_resp.json()["error"]["code"] == "CAPABILITY_NOT_SUPPORTED"

                invalid_query_resp = await client.get("/alpha/search")
                assert invalid_query_resp.status_code == 400
                assert invalid_query_resp.json()["error"]["code"] == "INVALID_PARAMS"

                unknown_resp = await client.get("/unknown/read")
                assert unknown_resp.status_code == 404

                sites_resp = await client.get("/api/sites")
                assert sites_resp.status_code == 200
                slugs = {site["slug"] for site in sites_resp.json()}
                assert slugs == {"alpha", "beta"}

                health_resp = await client.get("/health")
                assert health_resp.status_code == 200
                assert health_resp.json()["status"] == "ok"
                assert health_resp.json()["recipes"] == 2

                index_resp = await client.get("/")
                assert index_resp.status_code == 200
                assert "alpha" in index_resp.text
                assert "beta" in index_resp.text
                assert "/alpha/read" in index_resp.text

    assert any(
        getattr(record, "event", None) == "request.completed"
        and getattr(record, "path", None) == "/alpha/read"
        and getattr(record, "request_id", None) == "req-alpha-read"
        and isinstance(getattr(record, "response_time_ms", None), int)
        for record in caplog.records
    )
    assert any(
        getattr(record, "event", None) == "request.completed"
        and getattr(record, "path", None) == "/beta/search"
        and getattr(record, "status_code", None) == 400
        for record in caplog.records
    )

    assert fake_pool.started is True
    assert fake_pool.stopped is True
