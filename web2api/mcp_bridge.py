"""Legacy HTTP adapter for canonical Web2API MCP tools."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from web2api.mcp_utils import (
    ToolSpec,
    invoke_tool_spec,
    resolve_tool_spec,
    tool_result_value,
    tool_specs_from_registry,
)
from web2api.registry import RecipeRegistry
from web2api.schemas import status_code_for_error

logger = logging.getLogger(__name__)
FilterType = Literal["only", "exclude"]


def _filter_specs(
    specs: list[ToolSpec],
    *,
    filter_type: FilterType,
    slugs: set[str],
) -> list[ToolSpec]:
    if filter_type == "only":
        return [spec for spec in specs if spec.slug in slugs]
    return [spec for spec in specs if spec.slug not in slugs]


def register_mcp_routes(app: FastAPI) -> None:
    """Register the HTTP compatibility adapter for MCP tools."""

    @app.get("/mcp/tools")
    async def mcp_list_tools(
        request: Request,
        only: str | None = None,
        exclude: str | None = None,
    ) -> list[dict[str, Any]]:
        registry: RecipeRegistry = request.app.state.registry
        specs = tool_specs_from_registry(registry)
        if only:
            specs = _filter_specs(
                specs,
                filter_type="only",
                slugs={slug.strip() for slug in only.split(",") if slug.strip()},
            )
        if exclude:
            specs = _filter_specs(
                specs,
                filter_type="exclude",
                slugs={slug.strip() for slug in exclude.split(",") if slug.strip()},
            )
        return [spec.as_http_payload() for spec in specs]

    @app.get("/mcp/{filter_type}/{filter_value}/tools")
    async def mcp_list_tools_filtered(
        request: Request,
        filter_type: FilterType,
        filter_value: str,
    ) -> list[dict[str, Any]]:
        registry: RecipeRegistry = request.app.state.registry
        specs = _filter_specs(
            tool_specs_from_registry(registry),
            filter_type=filter_type,
            slugs={slug.strip() for slug in filter_value.split(",") if slug.strip()},
        )
        return [spec.as_http_payload() for spec in specs]

    @app.post("/mcp/{filter_type}/{filter_value}/tools/{tool_name}")
    async def mcp_call_tool_filtered(
        request: Request,
        filter_type: FilterType,
        filter_value: str,
        tool_name: str,
    ) -> JSONResponse:
        registry: RecipeRegistry = request.app.state.registry
        spec = resolve_tool_spec(registry, tool_name)
        slugs = {slug.strip() for slug in filter_value.split(",") if slug.strip()}
        if spec is None or (filter_type == "only" and spec.slug not in slugs):
            raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")
        if filter_type == "exclude" and spec.slug in slugs:
            raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")
        return await _call_tool(request, spec)

    @app.post("/mcp/tools/{tool_name}")
    async def mcp_call_tool(request: Request, tool_name: str) -> JSONResponse:
        registry: RecipeRegistry = request.app.state.registry
        spec = resolve_tool_spec(registry, tool_name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")
        return await _call_tool(request, spec)


async def _call_tool(request: Request, spec: ToolSpec) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    try:
        response = await invoke_tool_spec(request.app, spec, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP tool call failed: %s", spec.name)
        return JSONResponse({"result": f"Error: {exc}"}, status_code=500)

    response_data = response.model_dump(mode="json")
    return JSONResponse(
        {"result": tool_result_value(response_data)},
        status_code=status_code_for_error(response.error),
    )
