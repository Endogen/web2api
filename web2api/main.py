"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from web2api.engine import scrape
from web2api.pool import BrowserPool
from web2api.registry import Recipe, RecipeRegistry
from web2api.schemas import ErrorResponse

RouteEndpoint = Callable[..., Any]
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _default_recipes_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "recipes"


def _status_code_for_error(error: ErrorResponse | None) -> int:
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
    browser_pool = pool or BrowserPool()
    recipe_registry = registry or RecipeRegistry()
    recipe_registry.discover(recipes_dir or _default_recipes_dir())

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
