"""Core HTTP routes for discovery, health, scraping, and uploads."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from web2api.auth import AuthConfig, public_auth_payload
from web2api.execution import execute_recipe_endpoint
from web2api.plugin import build_plugin_payload
from web2api.registry import Recipe, RecipeRegistry
from web2api.schemas import status_code_for_error
from web2api.uploads import saved_uploads

MAX_QUERY_LENGTH = 16_384


def site_payload(recipe: Recipe, *, app_version: str) -> dict[str, Any]:
    """Build site metadata returned by discovery endpoints."""
    config = recipe.config
    plugin_payload = None
    if recipe.plugin is not None:
        plugin_payload = build_plugin_payload(
            recipe.plugin,
            current_web2api_version=app_version,
        )
    endpoints = [
        {
            "name": name,
            "description": endpoint.description,
            "requires_query": endpoint.requires_query,
            "link": f"/{config.slug}/{name}",
            "accepts_files": endpoint.accepts_files,
            "params": {
                param_name: param.model_dump(exclude_none=True)
                for param_name, param in endpoint.params.items()
            },
        }
        for name, endpoint in config.endpoints.items()
    ]
    return {
        "name": config.name,
        "slug": config.slug,
        "description": config.description,
        "base_url": config.base_url,
        "endpoints": endpoints,
        "plugin": plugin_payload,
    }


async def _serve_recipe_endpoint(
    request: Request,
    *,
    recipe: Recipe,
    endpoint_name: str,
    page: int,
    q: str | None,
    file_paths: list[str] | None = None,
) -> JSONResponse:
    response = await execute_recipe_endpoint(
        app=request.app,
        recipe=recipe,
        endpoint_name=endpoint_name,
        page=page,
        q=q,
        query_params=request.query_params,
        file_paths=file_paths,
    )
    return JSONResponse(
        content=response.model_dump(mode="json"),
        status_code=status_code_for_error(response.error),
    )


def register_api_routes(
    app: FastAPI,
    *,
    app_version: str,
    templates: Jinja2Templates,
    auth_config: AuthConfig,
) -> None:
    """Register the core application routes on *app*."""

    @app.get("/api/sites")
    async def list_sites(request: Request) -> list[dict[str, Any]]:
        registry: RecipeRegistry = request.app.state.registry
        return [site_payload(recipe, app_version=app_version) for recipe in registry.list_all()]

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        pool_health = request.app.state.pool.health
        pool_ready = bool(
            pool_health.get("ready", pool_health.get("browser_connected", False))
        )
        response_cache = request.app.state.response_cache
        cache_health = (
            {"enabled": False}
            if response_cache is None
            else await response_cache.stats()
        )
        registry: RecipeRegistry = request.app.state.registry
        payload = {
            "status": "ok" if pool_ready else "degraded",
            "pool": pool_health,
            "cache": cache_health,
            "recipes": registry.count,
        }
        return JSONResponse(
            content=payload,
            status_code=200 if pool_ready else 503,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        registry: RecipeRegistry = request.app.state.registry
        sites = [site_payload(recipe, app_version=app_version) for recipe in registry.list_all()]
        current_auth = getattr(request.app.state, "auth_config", auth_config)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"sites": sites, "auth": public_auth_payload(current_auth)},
        )

    @app.get("/{slug}/{endpoint}")
    async def recipe_endpoint(
        request: Request,
        slug: str,
        endpoint: str,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None, max_length=MAX_QUERY_LENGTH),
    ) -> JSONResponse:
        registry: RecipeRegistry = request.app.state.registry
        recipe = registry.get(slug)
        if recipe is None or endpoint not in recipe.config.endpoints:
            raise HTTPException(status_code=404, detail="Not Found")
        return await _serve_recipe_endpoint(
            request,
            recipe=recipe,
            endpoint_name=endpoint,
            page=page,
            q=q,
        )

    @app.post("/{slug}/{endpoint}")
    async def recipe_endpoint_post(
        request: Request,
        slug: str,
        endpoint: str,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None, max_length=MAX_QUERY_LENGTH),
        files: list[UploadFile] | None = File(default=None),
    ) -> JSONResponse:
        registry: RecipeRegistry = request.app.state.registry
        recipe = registry.get(slug)
        if recipe is None or endpoint not in recipe.config.endpoints:
            raise HTTPException(status_code=404, detail="Not Found")
        endpoint_config = recipe.config.endpoints[endpoint]
        async with saved_uploads(
            files,
            endpoint_name=endpoint,
            accepts_files=endpoint_config.accepts_files,
            max_files=request.app.state.max_upload_files,
            max_bytes=request.app.state.max_upload_bytes,
        ) as file_paths:
            return await _serve_recipe_endpoint(
                request,
                recipe=recipe,
                endpoint_name=endpoint,
                page=page,
                q=q,
                file_paths=file_paths,
            )
