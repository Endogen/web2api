"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

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
from web2api.schemas import ErrorResponse

RouteEndpoint = Callable[..., Awaitable[JSONResponse]]
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
logger = logging.getLogger(__name__)


def _default_recipes_dir() -> Path:
    """Return the default recipes directory path."""
    return Path(__file__).resolve().parent.parent / "recipes"


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
    return {
        "name": config.name,
        "slug": config.slug,
        "description": config.description,
        "base_url": config.base_url,
        "capabilities": config.capabilities,
        "links": {
            "read": f"/{config.slug}/read" if "read" in config.capabilities else None,
            "search": f"/{config.slug}/search" if "search" in config.capabilities else None,
        },
    }


def _create_recipe_handler(recipe: Recipe, endpoint: Literal["read", "search"]) -> RouteEndpoint:
    """Create a route handler bound to a specific recipe endpoint."""

    async def handler(
        request: Request,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None),
    ) -> JSONResponse:
        response = await scrape(
            pool=request.app.state.pool,
            recipe=recipe,
            endpoint=endpoint,
            page=page,
            query=q if endpoint == "search" else None,
        )
        return JSONResponse(
            content=response.model_dump(mode="json"),
            status_code=_status_code_for_error(response.error),
        )

    return handler


def _register_recipe_routes(app: FastAPI, registry: RecipeRegistry) -> None:
    """Register per-recipe read/search routes on the FastAPI app."""
    for recipe in registry.list_all():
        app.add_api_route(
            path=f"/{recipe.config.slug}/read",
            endpoint=_create_recipe_handler(recipe, "read"),
            methods=["GET"],
            name=f"{recipe.config.slug}_read",
        )
        app.add_api_route(
            path=f"/{recipe.config.slug}/search",
            endpoint=_create_recipe_handler(recipe, "search"),
            methods=["GET"],
            name=f"{recipe.config.slug}_search",
        )


def create_app(
    *,
    recipes_dir: Path | None = None,
    pool: BrowserPool | None = None,
    registry: RecipeRegistry | None = None,
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
    recipe_registry = registry or RecipeRegistry()
    effective_recipes_dir = recipes_dir
    if effective_recipes_dir is None:
        env_recipes = os.environ.get("RECIPES_DIR")
        effective_recipes_dir = Path(env_recipes) if env_recipes else _default_recipes_dir()
    recipe_registry.discover(effective_recipes_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await browser_pool.start()
        app.state.pool = browser_pool
        app.state.registry = recipe_registry
        try:
            yield
        finally:
            await browser_pool.stop()

    app = FastAPI(
        title="Web2API",
        summary="Turn websites into REST APIs by scraping them live with Playwright.",
        version="0.1.0",
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

    _register_recipe_routes(app, recipe_registry)

    @app.get("/api/sites")
    async def list_sites() -> list[dict[str, Any]]:
        """Return metadata for all discovered recipe sites."""
        return [_site_payload(recipe) for recipe in recipe_registry.list_all()]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Return service and browser pool health status."""
        return {
            "status": "ok",
            "pool": browser_pool.health,
            "recipes": recipe_registry.count,
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Render an index page listing all discovered recipe APIs."""
        sites = [_site_payload(recipe) for recipe in recipe_registry.list_all()]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"sites": sites},
        )

    return app


app = create_app()
