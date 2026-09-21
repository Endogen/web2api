"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from web2api import __version__
from web2api.auth import load_auth_config, public_auth_payload, request_is_authorized
from web2api.cache import CacheKey, ResponseCache
from web2api.config import ParamConfig, is_valid_param_name
from web2api.engine import scrape
from web2api.logging_utils import (
    REQUEST_ID_HEADER,
    build_request_id,
    log_event,
    reset_request_id,
    set_request_id,
)
from web2api.mcp_bridge import register_mcp_routes
from web2api.mcp_server import mount_mcp_server
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
_MAX_EXTRA_PARAM_VALUE_LENGTH = 512
_MAX_QUERY_LENGTH = 16_384
_UPLOAD_CHUNK_SIZE = 64 * 1024
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
            "accepts_files": ep_config.accepts_files,
            "params": {
                param_name: param.model_dump(exclude_none=True)
                for param_name, param in ep_config.params.items()
            },
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


def _collect_extra_params(
    query_params: Mapping[str, str],
) -> tuple[dict[str, str] | None, str | None]:
    extras: dict[str, str] = {}
    for key, value in query_params.items():
        if key in {"page", "q"}:
            continue
        if not is_valid_param_name(key):
            return None, (
                f"invalid query parameter '{key}': name must be a valid Python "
                "identifier and not a keyword"
            )
        if len(value) > _MAX_EXTRA_PARAM_VALUE_LENGTH:
            return None, (
                f"invalid query parameter '{key}': value length exceeds "
                f"{_MAX_EXTRA_PARAM_VALUE_LENGTH}"
            )
        extras[key] = value
    return extras or None, None


def _validate_declared_endpoint_params(
    *,
    recipe: Recipe,
    endpoint_name: str,
    extra_params: Mapping[str, str] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    endpoint_config = recipe.config.endpoints[endpoint_name]
    raw_params = dict(extra_params or {})
    unknown = sorted(set(raw_params) - set(endpoint_config.params))
    if unknown:
        return None, f"unknown query parameter(s): {', '.join(unknown)}"

    validated: dict[str, Any] = {}
    for param_name, param in endpoint_config.params.items():
        value = raw_params.get(param_name)
        if value is None or value == "":
            if param.required:
                return None, (
                    f"missing required query parameter '{param_name}' "
                    f"for endpoint '{endpoint_name}'"
                )
            continue
        try:
            validated[param_name] = _coerce_param_value(param_name, value, param)
        except ValueError as exc:
            return None, str(exc)
    return validated or None, None


def _coerce_param_value(name: str, raw: str, config: ParamConfig) -> Any:
    """Coerce and validate one declared endpoint parameter."""
    try:
        if config.type == "integer":
            value: Any = int(raw)
        elif config.type == "number":
            value = float(raw)
        elif config.type == "boolean":
            normalized = raw.strip().lower()
            if normalized not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError
            value = normalized in {"true", "1", "yes", "on"}
        else:
            value = raw
    except (TypeError, ValueError):
        raise ValueError(
            f"invalid query parameter '{name}': expected {config.type}"
        ) from None

    if config.enum is not None and value not in config.enum:
        allowed = ", ".join(repr(item) for item in config.enum)
        raise ValueError(f"invalid query parameter '{name}': expected one of {allowed}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if config.minimum is not None and value < config.minimum:
            raise ValueError(
                f"invalid query parameter '{name}': must be >= {config.minimum}"
            )
        if config.maximum is not None and value > config.maximum:
            raise ValueError(
                f"invalid query parameter '{name}': must be <= {config.maximum}"
            )
    if isinstance(value, str):
        if config.min_length is not None and len(value) < config.min_length:
            raise ValueError(
                f"invalid query parameter '{name}': length must be >= {config.min_length}"
            )
        if config.max_length is not None and len(value) > config.max_length:
            raise ValueError(
                f"invalid query parameter '{name}': length must be <= {config.max_length}"
            )
        if config.pattern is not None and re.fullmatch(config.pattern, value) is None:
            raise ValueError(
                f"invalid query parameter '{name}': does not match required pattern"
            )
    return value


def _sanitize_upload_filename(raw_filename: str, *, fallback_index: int) -> str:
    """Return a safe filename stripped of any path components."""
    normalized = raw_filename.replace("\\", "/")
    candidate = Path(normalized).name.strip()
    if not candidate or candidate in {".", ".."}:
        return f"upload_{fallback_index}"
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", candidate)[:255]
    if not sanitized or sanitized in {".", ".."}:
        return f"upload_{fallback_index}"
    return sanitized


def _cache_key_for_request(
    *,
    slug: str,
    endpoint: str,
    page: int,
    query: str | None,
    extra_params: dict[str, Any] | None,
) -> CacheKey:
    if extra_params:
        # Exclude non-hashable values (e.g. file_paths list) from cache key
        params = tuple(
            sorted(
                (k, str(v))
                for k, v in extra_params.items()
                if isinstance(v, (str, int, float, bool))
            )
        )
    else:
        params = ()
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
    """Serve a recipe endpoint request with shared execution logic."""
    response = await execute_recipe_endpoint(
        app=request.app,
        recipe=recipe,
        endpoint_name=endpoint_name,
        page=page,
        q=q,
        query_params=request.query_params,
        file_paths=getattr(getattr(request, "state", None), "file_paths", None),
    )
    return JSONResponse(
        content=response.model_dump(mode="json"),
        status_code=_status_code_for_error(response.error),
    )


async def execute_recipe_endpoint(
    *,
    app: FastAPI,
    recipe: Recipe,
    endpoint_name: str,
    page: int,
    q: str | None,
    query_params: Mapping[str, str],
    file_paths: list[str] | None = None,
) -> ApiResponse:
    """Execute a recipe endpoint request and return the normalized response."""
    raw_extra_params, extra_error = _collect_extra_params(query_params)
    if extra_error is not None:
        return _build_error_response(
            recipe=recipe,
            endpoint=endpoint_name,
            current_page=page,
            query=q,
            code="INVALID_PARAMS",
            message=extra_error,
        )
    extra_params, param_error = _validate_declared_endpoint_params(
        recipe=recipe,
        endpoint_name=endpoint_name,
        extra_params=raw_extra_params,
    )
    if param_error is not None:
        return _build_error_response(
            recipe=recipe,
            endpoint=endpoint_name,
            current_page=page,
            query=q,
            code="INVALID_PARAMS",
            message=param_error,
        )
    if file_paths:
        if not recipe.config.endpoints[endpoint_name].accepts_files:
            return _build_error_response(
                recipe=recipe,
                endpoint=endpoint_name,
                current_page=page,
                query=q,
                code="INVALID_PARAMS",
                message=f"endpoint '{endpoint_name}' does not accept file uploads",
            )
        if extra_params is None:
            extra_params = {}
        extra_params["file_paths"] = file_paths

    scrape_func = getattr(app.state, "scrape_func", scrape)

    async def _run_scrape() -> ApiResponse:
        kwargs: dict[str, Any] = {
            "pool": app.state.pool,
            "recipe": recipe,
            "endpoint": endpoint_name,
            "page": page,
            "query": q,
            "extra_params": extra_params,
            "scrape_timeout": app.state.scrape_timeout,
        }
        parameters = inspect.signature(scrape_func).parameters
        if "direct_semaphore" in parameters:
            kwargs["direct_semaphore"] = app.state.direct_scrape_semaphore
        if "allow_private_network" in parameters:
            kwargs["allow_private_network"] = app.state.allow_private_network
        return await scrape_func(**kwargs)

    response_cache: ResponseCache | None = getattr(app.state, "response_cache", None)
    cache_key: CacheKey | None = None
    # Skip cache for file upload requests.
    if file_paths:
        response_cache = None
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
            return _with_cached_metadata(cache_lookup.response)

    response = await _run_scrape()
    if response_cache is not None and cache_key is not None:
        await response_cache.set(cache_key, response)
    return response


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
    max_direct_scrapes = int(os.environ.get("DIRECT_MAX_CONCURRENCY", "20"))
    allow_private_network = _env_bool("WEB2API_ALLOW_PRIVATE_NETWORK", default=False)
    max_upload_files = int(os.environ.get("WEB2API_MAX_UPLOAD_FILES", "4"))
    max_upload_bytes = int(
        os.environ.get("WEB2API_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
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
    auth_config = load_auth_config()
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
        app.state.direct_scrape_semaphore = asyncio.Semaphore(max_direct_scrapes)
        app.state.allow_private_network = allow_private_network
        app.state.max_upload_files = max_upload_files
        app.state.max_upload_bytes = max_upload_bytes
        app.state.response_cache = active_response_cache
        app.state.catalog_source = catalog_source_value
        app.state.catalog_ref = catalog_ref_value
        app.state.catalog_path = catalog_path_value
        app.state.auth_config = auth_config
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
        auth_state = getattr(request.app.state, "auth_config", auth_config)
        try:
            if auth_state.admin_is_disabled(request.url.path):
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "Recipe administration is disabled until "
                            "WEB2API_ADMIN_TOKEN or WEB2API_ACCESS_TOKEN is configured."
                        ),
                    },
                )
                response.headers[REQUEST_ID_HEADER] = request_id
                log_event(
                    logger,
                    logging.WARNING,
                    "request.admin_disabled",
                    method=request.method,
                    path=request.url.path,
                    response_time_ms=elapsed_ms,
                )
                return response
            if auth_state.requires_auth(request.url.path) and not request_is_authorized(
                request.headers,
                auth_state,
                path=request.url.path,
            ):
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                response = JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Unauthorized. Provide Authorization: Bearer <token>.",
                    },
                    headers={"WWW-Authenticate": 'Bearer realm="web2api"'},
                )
                response.headers[REQUEST_ID_HEADER] = request_id
                log_event(
                    logger,
                    logging.WARNING,
                    "request.unauthorized",
                    method=request.method,
                    path=request.url.path,
                    response_time_ms=elapsed_ms,
                )
                return response
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
    register_mcp_routes(app)
    mount_mcp_server(app, registry=recipe_registry)

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
            context={
                "sites": sites,
                "auth": public_auth_payload(getattr(request.app.state, "auth_config", auth_config)),
            },
        )

    @app.get("/{slug}/{endpoint}")
    async def recipe_endpoint(
        request: Request,
        slug: str,
        endpoint: str,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None, max_length=_MAX_QUERY_LENGTH),
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

    @app.post("/{slug}/{endpoint}")
    async def recipe_endpoint_post(
        request: Request,
        slug: str,
        endpoint: str,
        page: int = Query(default=1, ge=1),
        q: str | None = Query(default=None, max_length=_MAX_QUERY_LENGTH),
        files: list[UploadFile] = File(default=[]),
    ) -> JSONResponse:
        """Serve recipe endpoints with file upload support (POST multipart)."""
        registry_state: RecipeRegistry = request.app.state.registry
        recipe = registry_state.get(slug)
        if recipe is None or endpoint not in recipe.config.endpoints:
            raise HTTPException(status_code=404, detail="Not Found")

        # Save uploaded files to temp directory
        import tempfile
        saved_paths: list[str] = []
        temp_dir = None
        try:
            if files:
                endpoint_config = recipe.config.endpoints[endpoint]
                if not endpoint_config.accepts_files:
                    raise HTTPException(
                        status_code=400,
                        detail=f"endpoint '{endpoint}' does not accept file uploads",
                    )
                if len(files) > request.app.state.max_upload_files:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "too many files; maximum is "
                            f"{request.app.state.max_upload_files}"
                        ),
                    )
                temp_dir = tempfile.mkdtemp(prefix="web2api_upload_")
                temp_dir_path = Path(temp_dir).resolve()
                for index, upload in enumerate(files):
                    if upload.filename:
                        safe_name = _sanitize_upload_filename(
                            upload.filename,
                            fallback_index=index,
                        )
                        dest = (temp_dir_path / safe_name).resolve()
                        if temp_dir_path not in dest.parents and dest != temp_dir_path:
                            raise HTTPException(status_code=400, detail="invalid upload filename")
                        with dest.open("wb") as f:
                            total_bytes = 0
                            while chunk := await upload.read(_UPLOAD_CHUNK_SIZE):
                                total_bytes += len(chunk)
                                if total_bytes > request.app.state.max_upload_bytes:
                                    raise HTTPException(
                                        status_code=413,
                                        detail=(
                                            f"file '{safe_name}' exceeds "
                                            f"{request.app.state.max_upload_bytes} bytes"
                                        ),
                                    )
                                f.write(chunk)
                        saved_paths.append(str(dest))

            # Inject file_paths into the request query string so
            # _collect_extra_params won't see it but the scraper will.
            # We pass it via a custom request state attribute instead.
            request.state.file_paths = saved_paths

            return await _serve_recipe_endpoint(
                request,
                recipe=recipe,
                endpoint_name=endpoint,
                page=page,
                q=q,
            )
        finally:
            # Clean up temp files
            if temp_dir:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)

    return app


app = create_app()
