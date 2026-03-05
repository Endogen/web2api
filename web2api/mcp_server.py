"""MCP protocol server — auto-exposes all web2api recipes as native MCP tools.

Each recipe endpoint becomes its own tool with proper name, description, and
typed parameters. Tools are rebuilt automatically when recipes change.

Clients connect via:
    claude mcp add --transport http web2api https://your-host/mcp/
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from web2api.mcp_utils import (
    TOOL_NAME_SEP,
    build_tool_name,
    format_tool_result,
    sites_from_registry,
)

logger = logging.getLogger(__name__)

WEB2API_INTERNAL_URL = "http://127.0.0.1:8000"

# Module-level state for cross-module access (recipe admin hooks).
_tool_registry: _ToolRegistry | None = None


class _ToolRegistry:
    """Manages dynamic tool registration on a FastMCP server."""

    def __init__(self, mcp: FastMCP, internal_url: str = WEB2API_INTERNAL_URL):
        self.mcp = mcp
        self.internal_url = internal_url
        self._registered_tools: set[str] = set()
        self._registry: Any = None  # Direct RecipeRegistry reference

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def build_tools(self) -> None:
        """(Re)build MCP tools from the current recipe registry."""
        if self._registry is None:
            logger.warning("No recipe registry available for MCP tool build")
            return

        sites = sites_from_registry(self._registry)
        self._clear_tools()
        self._register_all(sites)
        logger.info(
            "MCP tools built: %d tools from %d recipes",
            len(self._registered_tools),
            len(sites),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _clear_tools(self) -> None:
        for name in list(self._registered_tools):
            try:
                self.mcp.remove_tool(name)
            except Exception:
                pass
        self._registered_tools.clear()

    def _register_all(self, sites: list[dict]) -> None:
        for site in sites:
            slug = site["slug"]
            site_name = site["name"]
            base_url = site.get("base_url", "")

            for ep in site["endpoints"]:
                ep_name = ep["name"]
                ep_desc = ep.get("description", "")
                requires_q = ep.get("requires_query", False)
                ep_params = ep.get("params", {})

                tool_name = build_tool_name(slug, ep_name)
                desc = f"[{site_name}] {ep_desc}" if ep_desc else f"[{site_name}] {ep_name}"
                if base_url:
                    desc += f" ({base_url})"

                self._register_tool(
                    tool_name=tool_name,
                    description=desc,
                    slug=slug,
                    endpoint=ep_name,
                    requires_q=requires_q,
                    extra_params=ep_params,
                )
                self._registered_tools.add(tool_name)

    def _register_tool(
        self,
        *,
        tool_name: str,
        description: str,
        slug: str,
        endpoint: str,
        requires_q: bool,
        extra_params: dict[str, Any],
    ) -> None:
        # Capture for closure
        _slug, _endpoint, _url = slug, endpoint, self.internal_url

        # Build human-readable parameter docs
        param_docs: list[str] = []
        if requires_q:
            param_docs.append("q: The search query or prompt (required)")
        for pname, pcfg in extra_params.items():
            pdesc = pcfg.get("description", "")
            suffix = " (required)" if pcfg.get("required") else " (optional)"
            param_docs.append(f"{pname}: {pdesc}{suffix}")

        full_desc = description
        if param_docs:
            full_desc += "\n\nParameters:\n" + "\n".join(f"  - {p}" for p in param_docs)

        # --- tool function ---
        async def _fn(**kwargs: str) -> str:
            url = f"{_url}/{_slug}/{_endpoint}"
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

            return format_tool_result(resp.json())

        _fn.__name__ = tool_name
        _fn.__doc__ = full_desc

        # Build typed signature so the MCP SDK generates proper JSON Schema
        sig_params: list[inspect.Parameter] = []
        if requires_q:
            sig_params.append(
                inspect.Parameter("q", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
            )
        else:
            sig_params.append(
                inspect.Parameter("q", inspect.Parameter.POSITIONAL_OR_KEYWORD, default="", annotation=str)
            )
        for pname in extra_params:
            sig_params.append(
                inspect.Parameter(pname, inspect.Parameter.POSITIONAL_OR_KEYWORD, default="", annotation=str)
            )

        _fn.__signature__ = inspect.Signature(parameters=sig_params, return_annotation=str)
        _fn.__annotations__ = {"return": str}

        self.mcp.tool(name=tool_name, description=full_desc)(_fn)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def rebuild_mcp_tools() -> None:
    """Rebuild MCP tools from current recipes. Call after recipe changes."""
    if _tool_registry is not None:
        _tool_registry.build_tools()


def mount_mcp_server(app: Any, registry: Any = None) -> None:
    """Mount the MCP protocol server onto a FastAPI app at ``/mcp``.

    Args:
        app: The FastAPI application instance.
        registry: A populated ``RecipeRegistry`` to read recipes from.
    """
    global _tool_registry

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

    _tool_registry = _ToolRegistry(mcp)
    _tool_registry._registry = registry

    # Build tools now (registry is already populated at this point).
    _tool_registry.build_tools()

    # The MCP session manager must run within the app's lifespan.
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
