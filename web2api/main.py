"""FastAPI application factory and process entrypoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from web2api import __version__
from web2api.api_routes import register_api_routes
from web2api.cache import ResponseCache
from web2api.engine import scrape
from web2api.execution import execute_recipe_endpoint
from web2api.http_middleware import register_request_middleware
from web2api.mcp_bridge import register_mcp_routes
from web2api.mcp_server import mount_mcp_server
from web2api.pool import BrowserPool
from web2api.recipe_admin_api import register_recipe_admin_routes
from web2api.recipe_manager import default_recipes_dir
from web2api.registry import RecipeRegistry
from web2api.settings import AppSettings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
APP_VERSION = __version__


def create_app(
    *,
    recipes_dir: Path | None = None,
    pool: BrowserPool | None = None,
    registry: RecipeRegistry | None = None,
    scrape_timeout: float | None = None,
    response_cache: ResponseCache | None = None,
    settings: AppSettings | None = None,
) -> FastAPI:
    """Create and configure the Web2API application."""
    runtime = settings or AppSettings.from_env(default_recipes_dir=default_recipes_dir())
    logging.getLogger("web2api").setLevel(
        getattr(logging, runtime.log_level, logging.INFO)
    )

    browser_pool = pool or BrowserPool(
        max_contexts=runtime.pool_max_contexts,
        acquire_timeout=runtime.pool_acquire_timeout,
        page_timeout_ms=runtime.pool_page_timeout_ms,
        queue_size=runtime.pool_queue_size,
        proxy=runtime.browser_proxy,
    )
    effective_scrape_timeout = (
        runtime.scrape_timeout if scrape_timeout is None else scrape_timeout
    )
    effective_recipes_dir = recipes_dir or runtime.recipes_dir
    recipe_registry = registry or RecipeRegistry(
        app_version=APP_VERSION,
        enforce_plugin_compatibility=runtime.enforce_plugin_compatibility,
    )
    recipe_registry.discover(effective_recipes_dir)

    active_response_cache = response_cache
    if active_response_cache is None and runtime.cache_enabled:
        active_response_cache = ResponseCache(
            ttl_seconds=runtime.cache_ttl_seconds,
            stale_ttl_seconds=runtime.cache_stale_ttl_seconds,
            max_entries=runtime.cache_max_entries,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Chromium is intentionally lazy: direct-only installations do not pay
        # its startup or memory cost, and the first browser scrape starts it.
        app.state.pool = browser_pool
        app.state.registry = recipe_registry
        app.state.recipes_dir = effective_recipes_dir
        app.state.enforce_plugin_compatibility = runtime.enforce_plugin_compatibility
        app.state.scrape_timeout = effective_scrape_timeout
        app.state.direct_scrape_semaphore = asyncio.Semaphore(
            runtime.direct_max_concurrency
        )
        app.state.allow_private_network = runtime.allow_private_network
        app.state.max_upload_files = runtime.max_upload_files
        app.state.max_upload_bytes = runtime.max_upload_bytes
        app.state.response_cache = active_response_cache
        app.state.catalog_source = runtime.catalog_source
        app.state.catalog_ref = runtime.catalog_ref
        app.state.catalog_path = runtime.catalog_path
        app.state.auth_config = runtime.auth
        app.state.recipe_admin_lock = asyncio.Lock()
        app.state.scrape_func = scrape
        try:
            yield
        finally:
            await browser_pool.stop()

    app = FastAPI(
        title="Web2API",
        summary="Turn websites into REST APIs by scraping them live with Playwright.",
        version=APP_VERSION,
        lifespan=lifespan,
    )
    register_request_middleware(app, auth_config=runtime.auth)
    register_recipe_admin_routes(app, app_version=APP_VERSION)
    register_mcp_routes(app)
    mount_mcp_server(
        app,
        registry=recipe_registry,
        allowed_hosts=runtime.mcp_allowed_hosts,
        allowed_origins=runtime.mcp_allowed_origins,
    )
    register_api_routes(
        app,
        app_version=APP_VERSION,
        templates=TEMPLATES,
        auth_config=runtime.auth,
    )
    return app


app = create_app()

__all__ = ["APP_VERSION", "app", "create_app", "execute_recipe_endpoint"]
