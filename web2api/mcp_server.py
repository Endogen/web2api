"""Native MCP protocol adapter backed by canonical tool specifications."""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from web2api.mcp_utils import (
    ToolSpec,
    format_tool_result,
    invoke_tool_spec,
    tool_specs_from_registry,
)

logger = logging.getLogger(__name__)


def _annotation_for_schema(schema: dict[str, Any]) -> Any:
    base: Any = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }.get(schema.get("type", "string"), str)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        base = Literal.__getitem__(tuple(enum))
    field_args: dict[str, Any] = {}
    mapping = {
        "description": "description",
        "minimum": "ge",
        "maximum": "le",
        "pattern": "pattern",
        "minLength": "min_length",
        "maxLength": "max_length",
    }
    for source, target in mapping.items():
        if schema.get(source) is not None:
            field_args[target] = schema[source]
    return Annotated[base, Field(**field_args)] if field_args else base


class _ToolRegistry:
    """Manage dynamic FastMCP tools for one application instance."""

    def __init__(self, mcp: FastMCP, *, app: Any, bootstrap_registry: Any = None):
        self.mcp = mcp
        self.app = app
        self._bootstrap_registry = bootstrap_registry
        self._registered_tools: set[str] = set()

    def _current_registry(self) -> Any:
        live_registry = getattr(getattr(self.app, "state", None), "registry", None)
        return live_registry if live_registry is not None else self._bootstrap_registry

    def build_tools(self) -> None:
        """Rebuild tools from the application's current registry."""
        registry = self._current_registry()
        if registry is None:
            logger.warning("No recipe registry available for MCP tool build")
            return
        specs = tool_specs_from_registry(registry)
        self._clear_tools()
        for spec in specs:
            self._register_tool(spec)
            self._registered_tools.add(spec.name)
        logger.info(
            "MCP tools built: %d tools from %d recipes",
            len(specs),
            len({spec.slug for spec in specs}),
        )

    def _clear_tools(self) -> None:
        for name in list(self._registered_tools):
            try:
                self.mcp.remove_tool(name)
            except Exception:  # noqa: BLE001
                logger.debug("MCP tool was already absent: %s", name)
        self._registered_tools.clear()

    def _register_tool(self, spec: ToolSpec) -> None:
        properties = spec.parameters["properties"]
        required = set(spec.parameters.get("required", []))
        docs = []
        for name, schema in properties.items():
            suffix = "required" if name in required else "optional"
            description = schema.get("description", "")
            docs.append(f"{name}: {description} ({suffix})")
        full_description = spec.description
        if docs:
            full_description += "\n\nParameters:\n" + "\n".join(
                f"  - {line}" for line in docs
            )

        async def tool_function(**kwargs: Any) -> str:
            try:
                response = await invoke_tool_spec(self.app, spec, kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.exception("MCP protocol tool failed: %s", spec.name)
                return f"Error: {exc}"
            return format_tool_result(response.model_dump(mode="json"))

        tool_function.__name__ = spec.name
        tool_function.__doc__ = full_description
        parameters: list[inspect.Parameter] = []
        annotations: dict[str, Any] = {}
        extra_names = [name for name in properties if name not in {"q", "page"}]
        ordered_names = []
        if "q" in required:
            ordered_names.append("q")
        ordered_names.extend(name for name in extra_names if name in required)
        if "q" not in required:
            ordered_names.append("q")
        ordered_names.extend(name for name in extra_names if name not in required)
        ordered_names.append("page")
        for name in ordered_names:
            schema = properties[name]
            annotation = _annotation_for_schema(schema)
            default = inspect.Parameter.empty if name in required else schema.get("default")
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=annotation,
                )
            )
            annotations[name] = annotation
        tool_function.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            parameters=parameters,
            return_annotation=str,
        )
        tool_function.__annotations__ = {**annotations, "return": str}
        self.mcp.tool(name=spec.name, description=full_description)(tool_function)


def rebuild_mcp_tools(app: Any) -> None:
    """Rebuild native MCP tools for one application instance."""
    registry = getattr(getattr(app, "state", None), "mcp_tool_registry", None)
    if registry is not None:
        registry.build_tools()


def mount_mcp_server(
    app: Any,
    registry: Any = None,
    *,
    allowed_hosts: tuple[str, ...] = (
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
        "testserver",
    ),
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "http://localhost",
        "http://localhost:*",
    ),
) -> None:
    """Mount an application-scoped MCP protocol server at ``/mcp``."""
    mcp = FastMCP(
        "Web2API",
        instructions=(
            "Web2API exposes websites as API tools via live browser scraping. "
            "Each tool maps to a recipe endpoint and is fully self-describing."
        ),
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=list(allowed_origins),
        ),
    )
    tool_registry = _ToolRegistry(mcp, app=app, bootstrap_registry=registry)
    app.state.mcp_tool_registry = tool_registry
    tool_registry.build_tools()

    original_lifespan = getattr(app.router, "lifespan_context", None)

    @asynccontextmanager
    async def mcp_lifespan(a: Any):
        async with mcp.session_manager.run():
            if original_lifespan is not None:
                async with original_lifespan(a) as state:
                    yield state
            else:
                yield

    app.router.lifespan_context = mcp_lifespan
    app.mount("/mcp", mcp.streamable_http_app())
    logger.info("MCP protocol server mounted at /mcp")
