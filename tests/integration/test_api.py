"""Integration tests for API routes and index endpoints."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from web2api.cache import ResponseCache
from web2api.main import create_app
from web2api.recipe_manager import save_manifest
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
                "params": {
                    "tools_url": {
                        "description": "MCP bridge URL",
                        "required": False,
                        "example": "http://localhost:8100",
                    },
                },
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
                invalid_identifier_resp = await client.get("/alpha/read?model-id=1")
                assert invalid_identifier_resp.status_code == 400
                assert invalid_identifier_resp.json()["error"]["code"] == "INVALID_PARAMS"

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
                # Verify params are exposed in endpoint payload
                search_ep = next(
                    ep for ep in alpha_site["endpoints"] if ep["name"] == "search"
                )
                assert "params" in search_ep
                assert "tools_url" in search_ep["params"]
                assert search_ep["params"]["tools_url"]["description"] == "MCP bridge URL"
                assert search_ep["params"]["tools_url"]["required"] is False
                assert search_ep["params"]["tools_url"]["example"] == "http://localhost:8100"
                # Endpoint without params should have empty dict
                read_ep = next(
                    ep for ep in alpha_site["endpoints"] if ep["name"] == "read"
                )
                assert read_ep["params"] == {}
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
async def test_endpoint_requires_declared_required_extra_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(
        recipes_dir,
        "alpha",
        endpoints={
            "read": {
                "url": "https://example.com/items?page={page}",
                "params": {
                    "token": {
                        "description": "Required token",
                        "required": True,
                    },
                    "count": {
                        "description": "Bounded count",
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
                "pagination": {"type": "page_param", "param": "page"},
            },
        },
    )

    calls = 0
    captured: dict[str, object] = {}

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
        _ = pool, recipe, endpoint, query, scrape_timeout
        nonlocal calls
        calls += 1
        captured["extra_params"] = dict(extra_params or {})
        return _success_response(slug="alpha", endpoint="read", page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            missing_param = await client.get("/alpha/read")
            assert missing_param.status_code == 400
            assert missing_param.json()["error"]["code"] == "INVALID_PARAMS"
            assert "token" in missing_param.json()["error"]["message"]
            assert calls == 0

            success = await client.get("/alpha/read?token=secret")
            assert success.status_code == 200
            assert calls == 1

            typed = await client.get("/alpha/read?token=secret&count=5")
            assert typed.status_code == 200
            assert calls == 2

            invalid = await client.get("/alpha/read?token=secret&count=99")
            assert invalid.status_code == 400
            assert calls == 2

            unknown = await client.get("/alpha/read?token=secret&typo=value")
            assert unknown.status_code == 400
            assert "unknown query parameter" in unknown.json()["error"]["message"]
            assert calls == 2

    assert captured["extra_params"] == {"token": "secret", "count": 5}


@pytest.mark.asyncio
async def test_access_token_protects_all_routes_except_public_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "alpha")

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
        return _success_response(slug="alpha", endpoint="read", page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)
    monkeypatch.setenv("WEB2API_ACCESS_TOKEN", "secret-token")

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            public_resp = await client.get("/alpha/read")
            assert public_resp.status_code == 401

            sites_resp = await client.get("/api/sites")
            assert sites_resp.status_code == 401

            health_resp = await client.get("/health")
            assert health_resp.status_code == 200

            index_resp = await client.get("/")
            assert index_resp.status_code == 200
            assert "Paste access token" in index_resp.text
            assert "public paths shown below" in index_resp.text

            manage_resp = await client.get("/api/recipes/manage")
            assert manage_resp.status_code == 401
            assert manage_resp.headers["www-authenticate"] == 'Bearer realm="web2api"'

            updates_resp = await client.post("/api/recipes/manage/check-updates")
            assert updates_resp.status_code == 401

            mcp_tools_resp = await client.get("/mcp/tools")
            assert mcp_tools_resp.status_code == 401

            mcp_root_resp = await client.get("/mcp/")
            assert mcp_root_resp.status_code == 401

            authorized_manage = await client.get(
                "/api/recipes/manage",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert authorized_manage.status_code == 200

            alt_header_manage = await client.get(
                "/api/recipes/manage",
                headers={"X-Web2API-Key": "secret-token"},
            )
            assert alt_header_manage.status_code == 200

            authorized_mcp = await client.get(
                "/mcp/tools",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert authorized_mcp.status_code == 200

            authorized_sites = await client.get(
                "/api/sites",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert authorized_sites.status_code == 200

            authorized_recipe = await client.get(
                "/alpha/read",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert authorized_recipe.status_code == 200


@pytest.mark.asyncio
async def test_admin_routes_are_disabled_without_a_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB2API_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEB2API_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("WEB2API_ALLOW_UNAUTHENTICATED_ADMIN", raising=False)
    app = create_app(recipes_dir=tmp_path / "recipes", pool=FakePool())

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/recipes/manage")

    assert response.status_code == 403
    assert "disabled until" in response.json()["detail"]

@pytest.mark.asyncio
async def test_access_token_allows_configured_public_path_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(recipes_dir, "alpha")
    _write_recipe(recipes_dir, "beta")

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
        _ = pool, endpoint, query, extra_params, scrape_timeout
        return _success_response(slug=recipe.config.slug, endpoint="read", page=page)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)
    monkeypatch.setenv("WEB2API_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("WEB2API_PUBLIC_PATHS", "/api/sites,/alpha/*")

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            index_resp = await client.get("/")
            assert index_resp.status_code == 200
            assert "/alpha/*" in index_resp.text

            health_resp = await client.get("/health")
            assert health_resp.status_code == 200

            sites_resp = await client.get("/api/sites")
            assert sites_resp.status_code == 200

            alpha_resp = await client.get("/alpha/read")
            assert alpha_resp.status_code == 200

            beta_resp = await client.get("/beta/read")
            assert beta_resp.status_code == 401

            manage_resp = await client.get("/api/recipes/manage")
            assert manage_resp.status_code == 401

            authorized_beta = await client.get(
                "/beta/read",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert authorized_beta.status_code == 200


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
    missing_env = "WEB2API_TEST_GAMMA_TOKEN_UNLIKELY"
    monkeypatch.delenv(missing_env, raising=False)
    _write_recipe(
        catalog_root / "recipes",
        "gamma",
        plugin={
            "version": "1.0.0",
            "requires_env": [missing_env],
        },
    )

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
                        "docs_url": "https://example.com/gamma/readme",
                        "requires_env": [missing_env],
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
            assert payload_before["catalog"][0]["docs_url"] == "https://example.com/gamma/readme"
            assert payload_before["catalog"][0]["requires_env"] == [missing_env]
            assert payload_before["catalog"][0]["plugin"] is None

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
            assert gamma_catalog["docs_url"] == "https://example.com/gamma/readme"
            assert gamma_catalog["requires_env"] == [missing_env]
            assert gamma_catalog["plugin"] is not None
            assert gamma_catalog["plugin"]["status"]["checks"]["env"]["missing"] == [missing_env]

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


@pytest.mark.asyncio
async def test_mcp_bridge_preserves_special_characters_in_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(
        recipes_dir,
        "alpha",
        endpoints={
            "search": {
                "url": "https://example.com/search?q={query}&page={page}",
                "requires_query": True,
                "params": {
                    "tools_url": {
                        "description": "MCP bridge URL",
                        "required": False,
                    },
                },
                "items": {"container": ".item", "fields": {"title": {"selector": ".title"}}},
                "pagination": {"type": "page_param", "param": "page"},
            },
        },
    )

    captured: dict[str, object] = {}

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
        _ = pool, recipe, endpoint, page, scrape_timeout
        captured["query"] = query
        captured["extra_params"] = dict(extra_params or {})
        return _success_response(slug="alpha", endpoint="search", page=1, query=query)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/mcp/tools/alpha_search",
                json={
                    "q": "cats & dogs",
                    "tools_url": "http://localhost:8100/mcp/tools?x=1&y=2",
                },
            )

    assert response.status_code == 200
    assert captured["query"] == "cats & dogs"
    assert captured["extra_params"] == {
        "tools_url": "http://localhost:8100/mcp/tools?x=1&y=2",
    }


@pytest.mark.asyncio
async def test_post_upload_rejects_path_traversal_filenames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(
        recipes_dir,
        "alpha",
        endpoints={
            "read": {
                "url": "https://example.com/items?page={page}",
                "accepts_files": True,
                "items": {
                    "container": ".item",
                    "fields": {"title": {"selector": ".title"}},
                },
                "pagination": {"type": "page_param", "param": "page"},
            },
        },
    )

    captured: dict[str, object] = {}
    escaped_name = f"web2api_escape_{uuid.uuid4().hex}.txt"
    escaped_path = Path("/tmp") / escaped_name
    if escaped_path.exists():
        escaped_path.unlink()

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
        _ = pool, recipe, endpoint, page, query, scrape_timeout
        captured["extra_params"] = dict(extra_params or {})
        return _success_response(slug="alpha", endpoint="read", page=1)

    monkeypatch.setattr("web2api.main.scrape", fake_scrape)

    fake_pool = FakePool()
    app = create_app(recipes_dir=recipes_dir, pool=fake_pool)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/alpha/read",
                files={
                    "files": (f"../{escaped_name}", b"uploaded-content", "text/plain"),
                },
            )

    assert response.status_code == 200
    assert escaped_path.exists() is False
    extra_params = captured.get("extra_params")
    assert isinstance(extra_params, dict)
    file_paths = extra_params.get("file_paths", [])
    assert isinstance(file_paths, list)
    assert len(file_paths) == 1
    uploaded_path = Path(str(file_paths[0]))
    assert uploaded_path.name == escaped_name
    assert "/../" not in str(uploaded_path)


@pytest.mark.asyncio
async def test_post_upload_enforces_per_file_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "recipes"
    _write_recipe(
        recipes_dir,
        "alpha",
        endpoints={
            "read": {
                "url": "https://example.com/items?page={page}",
                "accepts_files": True,
                "items": {
                    "container": ".item",
                    "fields": {"title": {"selector": ".title"}},
                },
                "pagination": {"type": "page_param", "param": "page"},
            },
        },
    )
    monkeypatch.setenv("WEB2API_MAX_UPLOAD_BYTES", "3")
    app = create_app(recipes_dir=recipes_dir, pool=FakePool())

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/alpha/read",
                files={"files": ("large.txt", b"four", "text/plain")},
            )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_check_updates_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes_dir = tmp_path / "active-recipes"
    _write_recipe(recipes_dir, "alpha")

    # Pre-populate manifest with a known tree hash.
    save_manifest(
        recipes_dir,
        {
            "version": 1,
            "recipes": {
                "alpha": {
                    "folder": "alpha",
                    "source_type": "git",
                    "source": "https://example.com/repo.git",
                    "source_ref": None,
                    "source_subdir": None,
                    "trusted": True,
                    "installed_tree_hash": "aaa111",
                },
            },
        },
    )

    def _fake_run(command, check: bool, text: bool, **kwargs):  # noqa: ANN001
        del check, text
        command_list = [str(part) for part in command]
        if command_list[:3] == ["git", "init", "--quiet"]:
            Path(command_list[3]).mkdir(parents=True, exist_ok=True)
        elif "rev-parse" in command_list:
            return subprocess.CompletedProcess(command_list, 0, stdout="bbb222\n", stderr="")
        return subprocess.CompletedProcess(command_list, 0, stdout="", stderr="")

    monkeypatch.setattr("web2api.recipe_manager.subprocess.run", _fake_run)

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
            resp = await client.post("/api/recipes/manage/check-updates")
            assert resp.status_code == 200
            data = resp.json()
            assert "updates" in data
            assert data["updates"]["alpha"] is True
