"""MCP protocol server — auto-exposes all web2api recipes as native MCP tools.

Each recipe endpoint becomes its own tool with proper name, description, and
typed parameters. Tools are rebuilt automatically when recipes change.

Clients connect via:
    claude mcp add --transport http web2api https://web2api.endogen.dev/mcp/
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

TOOL_NAME_SEP = "__"
WEB2API_INTERNAL_URL = "http://127.0.0.1:8000"

# Global reference so we can trigger tool rebuild from admin routes
_mcp_instance: FastMCP | None = None
_tool_registry: _ToolRegistry | None = None


class _ToolRegistry:
    """Manages dynamic tool registration on a FastMCP server."""

    def __init__(self, mcp: FastMCP, internal_url: str = WEB2API_INTERNAL_URL):
        self.mcp = mcp
        self.internal_url = internal_url
        self._registered_tools: set[str] = set()
        self._app: Any = None  # Set after mount
        self._registry: Any = None  # Direct registry reference

    async def rebuild_tools(self) -> None:
        """Rebuild tools from the registry (direct) or via HTTP (fallback)."""
        sites = self._get_sites_from_registry()
        if sites is None:
            sites = await self._get_sites_from_http()
        if sites is None:
            return

    def _get_sites_from_registry(self) -> list[dict] | None:
        """Get recipe data directly from the app registry (no HTTP needed)."""
        try:
            registry = self._registry
            if registry is None and self._app is not None:
                registry = getattr(self._app.state, "registry", None)
            if registry is None:
                return None
            sites = []
            for recipe in registry.list_all():
                cfg = recipe.config
                endpoints = []
                for ep_name, ep_cfg in cfg.endpoints.items():
                    ep_params = {}
                    for pname, pcfg in ep_cfg.params.items():
                        ep_params[pname] = {
                            "description": pcfg.description,
                            "required": pcfg.required,
                            "example": pcfg.example,
                        }
                    endpoints.append({
                        "name": ep_name,
                        "description": ep_cfg.description,
                        "requires_query": ep_cfg.requires_query,
                        "params": ep_params,
                    })
                sites.append({
                    "slug": cfg.slug,
                    "name": cfg.name,
                    "description": cfg.description,
                    "endpoints": endpoints,
                })
            return sites
        except Exception as e:
            logger.debug("Could not read from registry directly: %s", e)
            return None

    async def _get_sites_from_http(self) -> list[dict] | None:
        """Fallback: fetch recipe data via HTTP."""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(f"{self.internal_url}/api/sites")
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.error("Failed to fetch recipes for MCP tool rebuild: %s", e)
                return None

        # Remove previously registered dynamic tools
        for tool_name in self._registered_tools:
            try:
                self.mcp.remove_tool(tool_name)
            except Exception:
                pass
        self._registered_tools.clear()

        # Register each endpoint as a tool
        for site in sites:
            slug = site.get("slug", "")
            site_name = site.get("name", slug)

            for ep in site.get("endpoints", []):
                ep_name = ep.get("name", "")
                ep_desc = ep.get("description", "")
                requires_q = ep.get("requires_query", False)
                ep_params = ep.get("params", {})

                tool_name = f"{slug}{TOOL_NAME_SEP}{ep_name}"
                description = f"[{site_name}] {ep_desc}" if ep_desc else f"[{site_name}] {ep_name}"

                # Build the tool function dynamically
                self._register_endpoint_tool(
                    tool_name=tool_name,
                    description=description,
                    slug=slug,
                    endpoint=ep_name,
                    requires_q=requires_q,
                    extra_params=ep_params,
                )
                self._registered_tools.add(tool_name)

        logger.info(
            "MCP tools rebuilt: %d tools from %d recipes",
            len(self._registered_tools),
            len(sites),
        )

    def _register_endpoint_tool(
        self,
        *,
        tool_name: str,
        description: str,
        slug: str,
        endpoint: str,
        requires_q: bool,
        extra_params: dict[str, Any],
    ) -> None:
        """Register a single recipe endpoint as an MCP tool."""

        # Capture variables for the closure
        _slug = slug
        _endpoint = endpoint
        _internal_url = self.internal_url

        # Build parameter docstring
        param_docs = []
        if requires_q:
            param_docs.append("q: The search query or prompt (required)")
        for pname, pcfg in extra_params.items():
            pdesc = pcfg.get("description", "")
            preq = pcfg.get("required", False)
            suffix = " (required)" if preq else " (optional)"
            param_docs.append(f"{pname}: {pdesc}{suffix}")

        full_description = description
        if param_docs:
            full_description += "\n\nParameters:\n" + "\n".join(f"  - {p}" for p in param_docs)

        async def _tool_fn(**kwargs: str) -> str:
            url = f"{_internal_url}/{_slug}/{_endpoint}"
            params: dict[str, str] = {"page": "1"}
            q = kwargs.get("q", "")
            if q:
                params["q"] = q
            for k, v in kwargs.items():
                if k != "q" and v:
                    params[k] = str(v)

            async with httpx.AsyncClient(timeout=120) as client:
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    return f"Error: HTTP {e.response.status_code} — {e.response.text[:500]}"
                except httpx.RequestError as e:
                    return f"Error: {e}"

            data = resp.json()
            error = data.get("error")
            if error:
                return f"Error: {error.get('message', 'unknown error')}"

            items = data.get("items", [])
            if not items:
                return "No results found."

            results = []
            for item in items:
                fields = item.get("fields", {})
                title = item.get("title", "")
                url_field = item.get("url", "")

                for key in ("response", "answer", "text", "content", "result"):
                    if key in fields:
                        if len(items) == 1:
                            return str(fields[key])
                        results.append(str(fields[key]))
                        break
                else:
                    parts = []
                    if title:
                        parts.append(f"**{title}**")
                    if url_field:
                        parts.append(url_field)
                    for k, v in fields.items():
                        parts.append(f"{k}: {v}")
                    results.append("\n".join(parts))

            return "\n\n---\n\n".join(results)

        # Set function metadata for MCP SDK
        _tool_fn.__name__ = tool_name
        _tool_fn.__doc__ = full_description

        # Build proper function signature for MCP schema generation
        import inspect

        params_list = []
        if requires_q:
            params_list.append(
                inspect.Parameter("q", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
            )
        else:
            params_list.append(
                inspect.Parameter("q", inspect.Parameter.POSITIONAL_OR_KEYWORD, default="", annotation=str)
            )

        for pname, pcfg in extra_params.items():
            params_list.append(
                inspect.Parameter(
                    pname,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default="",
                    annotation=str,
                )
            )

        _tool_fn.__signature__ = inspect.Signature(
            parameters=params_list,
            return_annotation=str,
        )
        _tool_fn.__annotations__ = {"return": str}

        # Register with FastMCP
        self.mcp.tool(name=tool_name, description=full_description)(_tool_fn)


def create_mcp_server() -> FastMCP:
    """Create a FastMCP server with dynamically registered recipe tools."""
    global _mcp_instance, _tool_registry

    mcp = FastMCP(
        "Web2API",
        instructions=(
            "Web2API exposes websites as API tools via live browser scraping. "
            "Each tool maps to a specific recipe endpoint. Tools are named "
            "{recipe}__{endpoint}. Use them directly — they are fully "
            "self-describing with typed parameters."
        ),
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    _mcp_instance = mcp
    _tool_registry = _ToolRegistry(mcp)

    return mcp


async def rebuild_mcp_tools() -> None:
    """Rebuild MCP tools from current recipes. Call after recipe changes."""
    _build_tools_sync()


def mount_mcp_server(app: Any, registry: Any = None) -> None:
    """Mount the MCP server onto a FastAPI/Starlette app at /mcp."""
    mcp = create_mcp_server()

    # Give the tool registry access to the app for direct registry reads
    if _tool_registry is not None:
        _tool_registry._app = app
        _tool_registry._registry = registry

    # Build tools synchronously from the registry right now
    _build_tools_sync()

    # The MCP session manager needs to run within the app's lifespan
    from contextlib import asynccontextmanager

    original_lifespan = getattr(app.router, "lifespan_context", None)

    @asynccontextmanager
    async def mcp_lifespan(a):
        async with mcp.session_manager.run():
            if original_lifespan is not None:
                async with original_lifespan(a) as state:
                    yield state
            else:
                yield

    app.router.lifespan_context = mcp_lifespan

    mcp_app = mcp.streamable_http_app()
    app.mount("/mcp", mcp_app)
    logger.info("MCP protocol server mounted at /mcp")


def _build_tools_sync() -> None:
    """Build tools from the registry synchronously (no HTTP needed)."""
    if _tool_registry is None:
        return
    sites = _tool_registry._get_sites_from_registry()
    if sites is None:
        logger.warning("No recipe registry available for MCP tool build")
        return

    # Remove old tools
    for tool_name in list(_tool_registry._registered_tools):
        try:
            _tool_registry.mcp.remove_tool(tool_name)
        except Exception:
            pass
    _tool_registry._registered_tools.clear()

    # Register new tools
    for site in sites:
        slug = site.get("slug", "")
        site_name = site.get("name", slug)
        for ep in site.get("endpoints", []):
            ep_name = ep.get("name", "")
            ep_desc = ep.get("description", "")
            requires_q = ep.get("requires_query", False)
            ep_params = ep.get("params", {})
            tool_name = f"{slug}{TOOL_NAME_SEP}{ep_name}"
            description = f"[{site_name}] {ep_desc}" if ep_desc else f"[{site_name}] {ep_name}"
            _tool_registry._register_endpoint_tool(
                tool_name=tool_name,
                description=description,
                slug=slug,
                endpoint=ep_name,
                requires_q=requires_q,
                extra_params=ep_params,
            )
            _tool_registry._registered_tools.add(tool_name)

    logger.info("MCP tools built: %d tools from %d recipes", len(_tool_registry._registered_tools), len(sites))
