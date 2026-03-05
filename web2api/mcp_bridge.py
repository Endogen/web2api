"""MCP HTTP bridge — auto-exposes all web2api recipes as MCP tools.

This is the legacy HTTP bridge for non-MCP clients and the web UI.
For MCP protocol clients, use the server at ``/mcp/`` instead.

Endpoints:
    GET  /mcp/tools          → list all recipe endpoints as tool definitions
    POST /mcp/tools/{name}   → call a tool (routes to the matching recipe endpoint)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from web2api.mcp_utils import build_tool_name, parse_tool_name
from web2api.registry import RecipeRegistry

logger = logging.getLogger(__name__)


def _build_tool_parameters(endpoint_cfg: Any) -> dict[str, Any]:
    """Build a JSON Schema for the tool's input parameters."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    if endpoint_cfg.requires_query:
        properties["q"] = {
            "type": "string",
            "description": "The search query or prompt.",
        }
        required.append("q")

    for param_name, param_cfg in endpoint_cfg.params.items():
        prop: dict[str, Any] = {"type": "string"}
        if param_cfg.description:
            prop["description"] = param_cfg.description
        if param_cfg.example:
            prop["examples"] = [param_cfg.example]
        properties[param_name] = prop
        if param_cfg.required:
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


def _tools_from_registry(registry: RecipeRegistry) -> list[dict[str, Any]]:
    """Generate MCP tool definitions from all registered recipes."""
    tools: list[dict[str, Any]] = []

    for recipe in registry.list_all():
        slug = recipe.config.slug
        site_name = recipe.config.name

        for ep_name, ep_cfg in recipe.config.endpoints.items():
            tool_name = build_tool_name(slug, ep_name)
            description = ep_cfg.description or f"{site_name} — {ep_name}"
            description = f"[{site_name}] {description}"

            tools.append({
                "name": tool_name,
                "description": description,
                "parameters": _build_tool_parameters(ep_cfg),
            })

    return tools


def register_mcp_routes(app: FastAPI) -> None:
    """Register the MCP HTTP bridge routes on the app."""

    @app.get("/mcp/tools")
    async def mcp_list_tools(
        request: Request,
        only: str | None = None,
        exclude: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all recipe endpoints as MCP tool definitions.

        Query params:
            only: comma-separated slugs to include (whitelist)
            exclude: comma-separated slugs to exclude (blacklist)
        """
        registry: RecipeRegistry = request.app.state.registry
        tools = _tools_from_registry(registry)

        only_set = {s.strip() for s in only.split(",") if s.strip()} if only else None
        exclude_set = {s.strip() for s in exclude.split(",") if s.strip()} if exclude else None

        if only_set:
            tools = [t for t in tools if parse_tool_name(t["name"])[0] in only_set]
        if exclude_set:
            tools = [t for t in tools if parse_tool_name(t["name"])[0] not in exclude_set]

        return tools

    @app.get("/mcp/{filter_type}/{filter_value}/tools")
    async def mcp_list_tools_filtered(
        request: Request,
        filter_type: str,
        filter_value: str,
    ) -> list[dict[str, Any]]:
        """List tools with path-based filtering.

        Examples:
            /mcp/only/brave-search,deepl/tools
            /mcp/exclude/allenai/tools
        """
        registry: RecipeRegistry = request.app.state.registry
        tools = _tools_from_registry(registry)

        slugs = {s.strip() for s in filter_value.split(",") if s.strip()}

        if filter_type == "only":
            tools = [t for t in tools if parse_tool_name(t["name"])[0] in slugs]
        elif filter_type == "exclude":
            tools = [t for t in tools if parse_tool_name(t["name"])[0] not in slugs]

        return tools

    @app.post("/mcp/{filter_type}/{filter_value}/tools/{tool_name}")
    async def mcp_call_tool_filtered(
        request: Request,
        filter_type: str,
        filter_value: str,
        tool_name: str,
    ) -> JSONResponse:
        """Call a tool via the filtered MCP path (routing is the same)."""
        return await mcp_call_tool(request, tool_name)

    @app.post("/mcp/tools/{tool_name}")
    async def mcp_call_tool(
        request: Request,
        tool_name: str,
    ) -> JSONResponse:
        """Call a recipe endpoint as an MCP tool.

        Accepts a JSON body with the tool parameters (e.g. ``{"q": "..."}``)
        and returns ``{"result": ...}`` with the scraped data.
        """
        parsed = parse_tool_name(tool_name)
        if parsed is None:
            raise HTTPException(status_code=404, detail=f"Invalid tool name: {tool_name}")

        slug, endpoint_name = parsed
        registry: RecipeRegistry = request.app.state.registry
        recipe = registry.get(slug)

        if recipe is None or endpoint_name not in recipe.config.endpoints:
            raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")

        try:
            body = await request.json()
        except Exception:
            body = {}

        if not isinstance(body, dict):
            body = {}

        query = body.pop("q", None) or body.pop("query", None)

        from web2api.main import _serve_recipe_endpoint

        params = {}
        if query:
            params["q"] = str(query)
        params["page"] = "1"
        for k, v in body.items():
            params[k] = str(v)

        scope = dict(request.scope)
        scope["query_string"] = "&".join(f"{k}={v}" for k, v in params.items()).encode()
        inner_request = Request(scope, request.receive)

        try:
            response = await _serve_recipe_endpoint(
                inner_request,
                recipe=recipe,
                endpoint_name=endpoint_name,
                page=1,
                q=query,
            )

            response_data = json.loads(response.body.decode())
            items = response_data.get("items", [])
            error = response_data.get("error")

            if error:
                return JSONResponse({"result": f"Error: {error.get('message', 'unknown error')}"})

            if len(items) == 1:
                fields = items[0].get("fields", {})
                for key in ("response", "answer", "text", "content", "result"):
                    if key in fields:
                        return JSONResponse({"result": fields[key]})
                return JSONResponse({"result": fields or items[0]})
            elif items:
                simplified = []
                for item in items:
                    entry: dict[str, Any] = {}
                    if item.get("title"):
                        entry["title"] = item["title"]
                    if item.get("url"):
                        entry["url"] = item["url"]
                    if item.get("fields"):
                        entry.update(item["fields"])
                    simplified.append(entry)
                return JSONResponse({"result": simplified})
            else:
                return JSONResponse({"result": "No results found."})

        except HTTPException as exc:
            return JSONResponse(
                {"result": f"Error: {exc.detail}"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            logger.exception("MCP tool call failed: %s", tool_name)
            return JSONResponse(
                {"result": f"Error: {exc}"},
                status_code=500,
            )
