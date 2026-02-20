"""Integration tests for API routes and index endpoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from web2api.cache import ResponseCache
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


def _write_recipe(
    recipes_dir: Path,
    slug: str,
    endpoints: dict[str, dict] | None = None,
    plugin: dict[str, object] | None = None,
) -> None:
    if endpoints is None:
        endpoints = {
            "read": {
                "url": "https://example.com/items?page={page}",
                "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
                "pagination": {"type": "page_param", "param": "page"},
            },
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
                "endpoints": endpoints,
            }
        ),
        encoding="utf-8",
    )
    if plugin is not None:
        (recipe_dir / "plugin.yaml").write_text(
            yaml.safe_dump(plugin),
            encoding="utf-8",
        )


def _success_response(
    *,
    slug: str,
    endpoint: str,
    page: int,
    query: str | None = None,
) -> ApiResponse:
    return ApiResponse(
        site=SiteInfo(name=slug.title(), slug=slug, url="https://example.com"),
        endpoint=endpoint,
        query=query,
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
    endpoint: str,
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
    missing_env = "WEB2API_TEST_ALPHA_TOKEN_UNLIKELY"
    monkeypatch.delenv(missing_env, raising=False)

    _write_recipe(
        recipes_dir,
        "alpha",
        endpoints={
            "read": {
                "url": "https://example.com/items?page={page}",
                "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
                "pagination": {"type": "page_param", "param": "page"},
            },
            "search": {
                "url": "https://example.com/search?q={query}&page={page}",
                "requires_query": True,
                "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
                "pagination": {"type": "page_param", "param": "page"},
            },
        },
        plugin={
            "version": "1.0.0",
            "requires_env": [missing_env],
            "dependencies": {"commands": ["missing-web2api-plugin-command"]},
        },
    )
    _write_recipe(recipes_dir, "beta")  # read only

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: str,
        page: int = 1,
        query: str | None = None,
        extra_params: dict[str, str] | None = None,
        scrape_timeout: float = 30.0,
    ) -> ApiResponse:
        _ = pool, extra_params, scrape_timeout
        ep_config = recipe.config.endpoints.get(endpoint)
        if ep_config is None:
            return _error_response(
                slug=recipe.config.slug,
                endpoint=endpoint,
                page=page,
                code="CAPABILITY_NOT_SUPPORTED",
                message="unsupported endpoint",
            )
        if ep_config.requires_query and not query:
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
                # Read endpoint
                read_resp = await client.get(
                    "/alpha/read?page=2",
                    headers={"x-request-id": "req-alpha-read"},
                )
                assert read_resp.status_code == 200
                assert read_resp.json()["endpoint"] == "read"
                assert read_resp.json()["pagination"]["current_page"] == 2
                assert read_resp.headers["x-request-id"] == "req-alpha-read"

                # Search endpoint
                search_resp = await client.get("/alpha/search?q=test&page=1")
                assert search_resp.status_code == 200
                assert search_resp.json()["endpoint"] == "search"
                assert search_resp.json()["query"] == "test"
                assert search_resp.headers["x-request-id"] != ""

                # Missing query on requires_query endpoint
                invalid_query_resp = await client.get("/alpha/search")
                assert invalid_query_resp.status_code == 400
                assert invalid_query_resp.json()["error"]["code"] == "INVALID_PARAMS"

                # Invalid extra query parameter name
                invalid_extra_resp = await client.get("/alpha/read?bad!param=1")
                assert invalid_extra_resp.status_code == 400
                assert invalid_extra_resp.json()["error"]["code"] == "INVALID_PARAMS"

                # Non-existent endpoint on a recipe (404 from FastAPI)
                unknown_ep_resp = await client.get("/beta/search")
                assert unknown_ep_resp.status_code == 404

                # Non-existent recipe (404 from FastAPI)
                unknown_resp = await client.get("/unknown/read")
                assert unknown_resp.status_code == 404

                # Sites listing
                sites_resp = await client.get("/api/sites")
                assert sites_resp.status_code == 200
                slugs = {site["slug"] for site in sites_resp.json()}
                assert slugs == {"alpha", "beta"}
                # Check endpoints structure in response
                alpha_site = next(s for s in sites_resp.json() if s["slug"] == "alpha")
                ep_names = {ep["name"] for ep in alpha_site["endpoints"]}
                assert ep_names == {"read", "search"}
                assert alpha_site["plugin"]["version"] == "1.0.0"
                assert alpha_site["plugin"]["status"]["ready"] is False
                assert missing_env in alpha_site["plugin"]["status"]["checks"]["env"]["missing"]
                assert (
                    "missing-web2api-plugin-command"
                    in alpha_site["plugin"]["status"]["checks"]["commands"]["missing"]
                )

                beta_site = next(s for s in sites_resp.json() if s["slug"] == "beta")
                assert beta_site["plugin"] is None

                # Health check
                health_resp = await client.get("/health")
                assert health_resp.status_code == 200
                assert health_resp.json()["status"] == "ok"
                assert health_resp.json()["recipes"] == 2
                assert health_resp.json()["cache"]["enabled"] is True

                # Index page
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

    assert fake_pool.started is True
    assert fake_pool.stopped is True


@pytest.mark.asyncio
async def test_response_cache_serves_fresh_and_stale_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "alpha")

    call_count = 0

    async def fake_scrape(
        *,
        pool: FakePool,
        recipe,
        endpoint: str,
        page: int = 1,
        query: str | None = None,
        extra_params: dict[str, str] | None = None,
        scrape_timeout: float = 30.0,
    ) -> ApiResponse:
        _ = pool, recipe, endpoint, query, extra_params, scrape_timeout
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.02)
        return _success_response(slug="alpha", endpoint="read", page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)
    fake_pool = FakePool()
    app = create_app(
        recipes_dir=recipes_dir,
        pool=fake_pool,
        response_cache=ResponseCache(
            ttl_seconds=0.05,
            stale_ttl_seconds=0.25,
            max_entries=16,
        ),
    )

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.get("/alpha/read?page=1")
            assert first.status_code == 200
            assert first.json()["metadata"]["cached"] is False

            second = await client.get("/alpha/read?page=1")
            assert second.status_code == 200
            assert second.json()["metadata"]["cached"] is True
            assert call_count == 1

            await asyncio.sleep(0.06)
            stale = await client.get("/alpha/read?page=1")
            assert stale.status_code == 200
            assert stale.json()["metadata"]["cached"] is True

            await asyncio.sleep(0.15)
            assert call_count >= 2


@pytest.mark.asyncio
async def test_recipe_management_api_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "active-recipes"
    catalog_root = tmp_path / "catalog-src"
    _write_recipe(catalog_root / "recipes", "gamma")

    catalog_file = catalog_root / "catalog.yaml"
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    catalog_file.write_text(
        yaml.safe_dump(
            {
                "recipes": {
                    "gamma": {
                        "source": "./recipes/gamma",
                        "trusted": True,
                        "description": "Gamma recipe",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("WEB2API_RECIPE_CATALOG_SOURCE", str(catalog_file))
    monkeypatch.delenv("WEB2API_RECIPE_CATALOG_REF", raising=False)
    monkeypatch.delenv("WEB2API_RECIPE_CATALOG_PATH", raising=False)

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            manage_before = await client.get("/api/recipes/manage")
            assert manage_before.status_code == 200
            payload_before = manage_before.json()
            assert payload_before["catalog_error"] is None
            assert payload_before["catalog"][0]["name"] == "gamma"
            assert payload_before["catalog"][0]["installed"] is False

            install_resp = await client.post("/api/recipes/manage/install/gamma")
            assert install_resp.status_code == 200
            assert install_resp.json()["slug"] == "gamma"

            sites_after_install = await client.get("/api/sites")
            assert sites_after_install.status_code == 200
            slugs_after_install = {site["slug"] for site in sites_after_install.json()}
            assert "gamma" in slugs_after_install

            disable_resp = await client.post("/api/recipes/manage/disable/gamma")
            assert disable_resp.status_code == 200

            manage_after_disable = await client.get("/api/recipes/manage")
            assert manage_after_disable.status_code == 200
            gamma_catalog = next(
                item for item in manage_after_disable.json()["catalog"] if item["name"] == "gamma"
            )
            assert gamma_catalog["installed"] is True
            assert gamma_catalog["enabled"] is False

            enable_resp = await client.post("/api/recipes/manage/enable/gamma")
            assert enable_resp.status_code == 200

            update_resp = await client.post("/api/recipes/manage/update/gamma")
            assert update_resp.status_code == 200
            assert update_resp.json()["slug"] == "gamma"

            uninstall_resp = await client.post("/api/recipes/manage/uninstall/gamma")
            assert uninstall_resp.status_code == 200

            sites_after_uninstall = await client.get("/api/sites")
            assert sites_after_uninstall.status_code == 200
            slugs_after_uninstall = {site["slug"] for site in sites_after_uninstall.json()}
            assert "gamma" not in slugs_after_uninstall


@pytest.mark.asyncio
async def test_recipe_management_uninstall_force_for_unmanaged_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "active-recipes"
    _write_recipe(recipes_dir, "local-only")

    catalog_file = tmp_path / "catalog.yaml"
    catalog_file.write_text(yaml.safe_dump({"recipes": {}}), encoding="utf-8")

    monkeypatch.setenv("WEB2API_RECIPE_CATALOG_SOURCE", str(catalog_file))
    monkeypatch.delenv("WEB2API_RECIPE_CATALOG_REF", raising=False)
    monkeypatch.delenv("WEB2API_RECIPE_CATALOG_PATH", raising=False)

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            manage_resp = await client.get("/api/recipes/manage")
            assert manage_resp.status_code == 200
            installed = manage_resp.json()["installed"]
            local_entry = next(item for item in installed if item["slug"] == "local-only")
            assert local_entry["managed"] is False
            assert local_entry["origin"] == "unmanaged"

            uninstall_without_force = await client.post("/api/recipes/manage/uninstall/local-only")
            assert uninstall_without_force.status_code == 400

            uninstall_force = await client.post(
                "/api/recipes/manage/uninstall/local-only?force=true"
            )
            assert uninstall_force.status_code == 200
            assert uninstall_force.json()["forced"] is True

            sites_after_uninstall = await client.get("/api/sites")
            assert sites_after_uninstall.status_code == 200
            slugs = {site["slug"] for site in sites_after_uninstall.json()}
            assert "local-only" not in slugs
