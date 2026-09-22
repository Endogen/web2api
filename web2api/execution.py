"""Shared validation and execution for HTTP and MCP recipe calls."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from web2api.cache import CacheKey, ResponseCache
from web2api.config import ParamConfig, is_valid_param_name
from web2api.engine import scrape
from web2api.registry import Recipe
from web2api.schemas import (
    ApiResponse,
    ErrorCode,
    ErrorResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)

_MAX_EXTRA_PARAM_VALUE_LENGTH = 512

def _build_error_response(
    *,
    recipe: Recipe,
    endpoint: str,
    current_page: int,
    query: str | None,
    code: ErrorCode,
    message: str,
) -> ApiResponse:
    endpoint_config = recipe.config.endpoints.get(endpoint)
    requires_query = endpoint_config.requires_query if endpoint_config is not None else False
    return ApiResponse(
        site=SiteInfo(
            name=recipe.config.name,
            slug=recipe.config.slug,
            url=recipe.config.base_url,
        ),
        endpoint=endpoint,
        query=query if requires_query else None,
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
        return await scrape_func(
            pool=app.state.pool,
            recipe=recipe,
            endpoint=endpoint_name,
            page=page,
            query=q,
            extra_params=extra_params,
            scrape_timeout=app.state.scrape_timeout,
            direct_semaphore=app.state.direct_scrape_semaphore,
            allow_private_network=app.state.allow_private_network,
        )

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
