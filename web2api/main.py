"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from web2api import __version__
from web2api.cache import CacheKey, ResponseCache
from web2api.engine import scrape
from web2api.logging_utils import (
    REQUEST_ID_HEADER,
    build_request_id,
    log_event,
    reset_request_id,
    set_request_id,
)
from web2api.plugin import build_plugin_payload
from web2api.pool import BrowserPool
from web2api.recipe_admin_api import register_recipe_admin_routes
from web2api.recipe_manager import (
    default_catalog_path,
    default_catalog_ref,
    default_catalog_source,
    default_recipes_dir,
)
from web2api.registry import Recipe, RecipeRegistry
from web2api.schemas import (
    ApiResponse,
    ErrorCode,
    ErrorResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
logger = logging.getLogger(__name__)
_EXTRA_PARAM_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_MAX_EXTRA_PARAM_VALUE_LENGTH = 512
APP_VERSION = __version__


def _default_recipes_dir() -> Path:
    """Return the default recipes directory path."""
    return default_recipes_dir()


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _status_code_for_error(error: ErrorResponse | None) -> int:
    """Map unified API error payloads to HTTP status codes."""
    if error is None:
        return 200
    return {
        "SITE_NOT_FOUND": 404,
        "CAPABILITY_NOT_SUPPORTED": 400,
        "INVALID_PARAMS": 400,
        "SCRAPE_FAILED": 502,
        "SCRAPE_TIMEOUT": 504,
        "INTERNAL_ERROR": 500,
    }.get(error.code, 500)


def _site_payload(recipe: Recipe) -> dict[str, Any]:
    """Build the site metadata payload returned by discovery endpoints."""
    config = recipe.config
    plugin_payload = None
    if recipe.plugin is not None:
        plugin_payload = build_plugin_payload(
            recipe.plugin,
            current_web2api_version=APP_VERSION,
        )
    endpoints_info: list[dict[str, Any]] = []
    for name, ep_config in config.endpoints.items():
        endpoints_info.append({
            "name": name,
            "description": ep_config.description,
            "requires_query": ep_config.requires_query,
            "link": f"/{config.slug}/{name}",
        })
    return {
        "name": config.name,
        "slug": config.slug,
        "description": config.description,
        "base_url": config.base_url,
        "endpoints": endpoints_info,
        "plugin": plugin_payload,
    }


def _build_error_response(
    *,
    recipe: Recipe,
    endpoint: str,
    current_page: int,
    query: str | None,
    code: ErrorCode,
    message: str,
) -> ApiResponse:
    return ApiResponse(
        site=SiteInfo(
            name=recipe.config.name,
            slug=recipe.config.slug,
            url=recipe.config.base_url,
        ),
        endpoint=endpoint,
        query=query if recipe.config.endpoints[endpoint].requires_query else None,
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
            response_time_ms=0,
            item_count=0,
            cached=False,
        ),
        error=ErrorResponse(code=code, message=message, details=None),
    )


def _collect_extra_params(request: Request) -> tuple[dict[str, str] | None, str | None]:
    extras: dict[str, str] = {}
    for key, value in request.query_params.items():
        if key in {"page", "q"}:
            continue
        if not _EXTRA_PARAM_PATTERN.match(key):
            return None, (
                f"invalid query parameter '{key}': names must match "
                "[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}"
            )
        if len(value) > _MAX_EXTRA_PARAM_VALUE_LENGTH:
            return None, (
                f"invalid query parameter '{key}': value length exceeds "
                f"{_MAX_EXTRA_PARAM_VALUE_LENGTH}"
            )
        extras[key] = value
    return extras or None, None


def _cache_key_for_request(
    *,
    slug: str,
    endpoint: str,
    page: int,
    query: str | None,
    extra_params: dict[str, str] | None,
) -> CacheKey:
    params = tuple(sorted(extra_params.items())) if extra_params else ()
    return (slug, endpoint, page, query, params)


def _with_cached_metadata(response: ApiResponse) -> ApiResponse:
    cached_response = response.model_copy(deep=True)
    cached_response.metadata.cached = True
    return cached_response


async def _serve_recipe_endpoint(
    request: Request,
    *,
    recipe: Recipe,
    endpoint_name: str,
    page: int,
    q: str | None,
) -> JSONResponse:
    """Serve a recipe endpoint request with cache support."""
    extra_params, extra_error = _collect_extra_params(request)
    if extra_error is not None:
        response = _build_error_response(
            recipe=recipe,
            endpoint=endpoint_name,
            current_page=page,
            query=q,
            code="INVALID_PARAMS",
            message=extra_error,
        )
        return JSONResponse(
            content=response.model_dump(mode="json"),
            status_code=_status_code_for_error(response.error),
        )

    async def _run_scrape() -> ApiResponse:
        return await scrape(
            pool=request.app.state.pool,
            recipe=recipe,
            endpoint=endpoint_name,
            page=page,
            query=q,
            extra_params=extra_params,
            scrape_timeout=request.app.state.scrape_timeout,
        )

    response_cache: ResponseCache | None = getattr(request.app.state, "response_cache", None)
    cache_key: CacheKey | None = None
    if response_cache is not None:
        cache_key = _cache_key_for_request(
            slug=recipe.config.slug,
            endpoint=endpoint_name,
            page=page,
            query=q,
            extra_params=extra_params,
        )
        cache_lookup = await response_cache.get(cache_key)
        if cache_lookup.response is not None:
            if cache_lookup.state == "stale":
                await response_cache.trigger_refresh(cache_key, _run_scrape)
            cached_response = _with_cached_metadata(cache_lookup.response)
            return JSONResponse(
                content=cached_response.model_dump(mode="json"),
                status_code=_status_code_for_error(cached_response.error),
            )

    response = await _run_scrape()
    if response_cache is not None and cache_key is not None:
        await response_cache.set(cache_key, response)
    return JSONResponse(
        content=response.model_dump(mode="json"),
        status_code=_status_code_for_error(response.error),
    )


def create_app(
    *,
    recipes_dir: Path | None = None,
    pool: BrowserPool | None = None,
    registry: RecipeRegistry | None = None,
    scrape_timeout: float | None = None,
    response_cache: ResponseCache | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    logging.getLogger("web2api").setLevel(logging.INFO)
    browser_pool = pool or BrowserPool(
        max_contexts=int(os.environ.get("POOL_MAX_CONTEXTS", "5")),
        context_ttl=int(os.environ.get("POOL_CONTEXT_TTL", "50")),
        acquire_timeout=float(os.environ.get("POOL_ACQUIRE_TIMEOUT", "30.0")),
        page_timeout_ms=int(os.environ.get("POOL_PAGE_TIMEOUT", "15000")),
        queue_size=int(os.environ.get("POOL_QUEUE_SIZE", "20")),
    )
    effective_scrape_timeout = (
        scrape_timeout
        if scrape_timeout is not None
        else float(os.environ.get("SCRAPE_TIMEOUT", "30"))
    )
    enforce_plugin_compatibility = _env_bool(
        "PLUGIN_ENFORCE_COMPATIBILITY",
        default=False,
    )
    recipe_registry = registry or RecipeRegistry(
        app_version=APP_VERSION,
        enforce_plugin_compatibility=enforce_plugin_compatibility,
    )
    effective_recipes_dir = recipes_dir
    if effective_recipes_dir is None:
        env_recipes = os.environ.get("RECIPES_DIR")
        effective_recipes_dir = Path(env_recipes) if env_recipes else _default_recipes_dir()
    recipe_registry.discover(effective_recipes_dir)

    catalog_source_value = default_catalog_source()
    catalog_ref_value = default_catalog_ref()
    catalog_path_value = default_catalog_path()
    cache_enabled = _env_bool("CACHE_ENABLED", default=True)
    active_response_cache = response_cache
    if active_response_cache is None and cache_enabled:
        active_response_cache = ResponseCache(
            ttl_seconds=float(os.environ.get("CACHE_TTL_SECONDS", "30")),
            stale_ttl_seconds=float(os.environ.get("CACHE_STALE_TTL_SECONDS", "120")),
            max_entries=int(os.environ.get("CACHE_MAX_ENTRIES", "500")),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await browser_pool.start()
        app.state.pool = browser_pool
        app.state.registry = recipe_registry
        app.state.recipes_dir = effective_recipes_dir
        app.state.enforce_plugin_compatibility = enforce_plugin_compatibility
        app.state.scrape_timeout = effective_scrape_timeout
        app.state.response_cache = active_response_cache
        app.state.catalog_source = catalog_source_value
        app.state.catalog_ref = catalog_ref_value
        app.state.catalog_path = catalog_path_value
        app.state.recipe_admin_lock = asyncio.Lock()
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

    @app.middleware("http")
    async def request_logging_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = build_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = set_request_id(request_id)
        request.state.request_id = request_id
        started_at = perf_counter()
        log_event(
            logger,
            logging.INFO,
            "request.started",
            method=request.method,
            path=request.url.path,
        )
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            log_event(
                logger,
                logging.ERROR,
                "request.failed",
                method=request.method,
                path=request.url.path,
                response_time_ms=elapsed_ms,
                error=str(exc),
                exc_info=exc,
            )
            raise
        else:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            response.headers[REQUEST_ID_HEADER] = request_id
            log_event(
                logger,
                logging.INFO,
                "request.completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
            )
            return response
        finally:
            reset_request_id(token)

    @app.get("/api/sites")
    async def list_sites(request: Request) -> list[dict[str, Any]]:
        """Return metadata for all discovered recipe sites."""
        registry_state: RecipeRegistry = request.app.state.registry
        return [_site_payload(recipe) for recipe in registry_state.list_all()]

    register_recipe_admin_routes(app, app_version=APP_VERSION)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        """Return service and browser pool health status."""
        pool_health = browser_pool.health
        cache_health: dict[str, int | float | bool]
        if active_response_cache is None:
            cache_health = {"enabled": False}
        else:
            cache_health = await active_response_cache.stats()
        registry_state: RecipeRegistry = request.app.state.registry

        if not pool_health["browser_connected"]:
            return JSONResponse(
                content={
                    "status": "degraded",
                    "pool": pool_health,
                    "cache": cache_health,
                    "recipes": registry_state.count,
                },
                status_code=503,
            )
        return JSONResponse(
            content={
                "status": "ok",
                "pool": pool_health,
                "cache": cache_health,
                "recipes": registry_state.count,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Render an index page listing all discovered recipe APIs."""
        registry_state: RecipeRegistry = request.app.state.registry
        sites = [_site_payload(recipe) for recipe in registry_state.list_all()]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"sites": sites},
        )

    @app.get("/{slug}/{endpoint}")
    async def recipe_endpoint(
        request: Request,
        slug: str,
        endpoint: str,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None),
    ) -> JSONResponse:
        """Serve recipe endpoints using the live in-memory registry."""
        registry_state: RecipeRegistry = request.app.state.registry
        recipe = registry_state.get(slug)
        if recipe is None or endpoint not in recipe.config.endpoints:
            raise HTTPException(status_code=404, detail="Not Found")
        return await _serve_recipe_endpoint(
            request,
            recipe=recipe,
            endpoint_name=endpoint,
            page=page,
            q=q,
        )

    return app


app = create_app()
