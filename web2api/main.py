"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from web2api.cache import CacheKey, ResponseCache
from web2api.engine import scrape
from web2api.logging_utils import (
    REQUEST_ID_HEADER,
    build_request_id,
    log_event,
    reset_request_id,
    set_request_id,
)
from web2api.pool import BrowserPool
from web2api.registry import Recipe, RecipeRegistry
from web2api.schemas import (
    ApiResponse,
    ErrorCode,
    ErrorResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)

RouteEndpoint = Callable[..., Awaitable[JSONResponse]]
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
logger = logging.getLogger(__name__)
_EXTRA_PARAM_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_MAX_EXTRA_PARAM_VALUE_LENGTH = 512


def _default_recipes_dir() -> Path:
    """Return the default recipes directory path."""
    return Path(__file__).resolve().parent.parent / "recipes"


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


def _create_recipe_handler(recipe: Recipe, endpoint_name: str) -> RouteEndpoint:
    """Create a route handler bound to a specific recipe endpoint."""

    async def handler(
        request: Request,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None),
    ) -> JSONResponse:
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

    return handler


def _register_recipe_routes(app: FastAPI, registry: RecipeRegistry) -> None:
    """Register per-recipe endpoint routes on the FastAPI app."""
    for recipe in registry.list_all():
        for endpoint_name in recipe.config.endpoints:
            app.add_api_route(
                path=f"/{recipe.config.slug}/{endpoint_name}",
                endpoint=_create_recipe_handler(recipe, endpoint_name),
                methods=["GET"],
                name=f"{recipe.config.slug}_{endpoint_name}",
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
    recipe_registry = registry or RecipeRegistry()
    effective_recipes_dir = recipes_dir
    if effective_recipes_dir is None:
        env_recipes = os.environ.get("RECIPES_DIR")
        effective_recipes_dir = Path(env_recipes) if env_recipes else _default_recipes_dir()
    recipe_registry.discover(effective_recipes_dir)
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
        app.state.scrape_timeout = effective_scrape_timeout
        app.state.response_cache = active_response_cache
        try:
            yield
        finally:
            await browser_pool.stop()

    app = FastAPI(
        title="Web2API",
        summary="Turn websites into REST APIs by scraping them live with Playwright.",
        version="0.2.0",
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
    async def list_sites() -> list[dict[str, Any]]:
        """Return metadata for all discovered recipe sites."""
        return [_site_payload(recipe) for recipe in recipe_registry.list_all()]

    @app.get("/health")
    async def health() -> JSONResponse:
        """Return service and browser pool health status."""
        pool_health = browser_pool.health
        cache_health: dict[str, int | float | bool]
        if active_response_cache is None:
            cache_health = {"enabled": False}
        else:
            cache_health = await active_response_cache.stats()

        if not pool_health["browser_connected"]:
            return JSONResponse(
                content={
                    "status": "degraded",
                    "pool": pool_health,
                    "cache": cache_health,
                    "recipes": recipe_registry.count,
                },
                status_code=503,
            )
        return JSONResponse(
            content={
                "status": "ok",
                "pool": pool_health,
                "cache": cache_health,
                "recipes": recipe_registry.count,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Render an index page listing all discovered recipe APIs."""
        sites = [_site_payload(recipe) for recipe in recipe_registry.list_all()]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"sites": sites},
        )

    # Register dynamic recipe routes after framework routes so static endpoints
    # keep precedence even if a recipe path shape overlaps.
    _register_recipe_routes(app, recipe_registry)

    return app


app = create_app()
