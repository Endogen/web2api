"""Canonical MCP tool specifications and invocation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web2api.config import EndpointConfig

TOOL_NAME_SEP = "__"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One recipe endpoint exposed consistently through both MCP adapters."""

    name: str
    description: str
    slug: str
    endpoint: str
    parameters: dict[str, Any]
    endpoint_config: EndpointConfig

    def as_http_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "slug": self.slug,
        }


def build_tool_name(slug: str, endpoint: str, override: str | None = None) -> str:
    """Build the canonical tool name for a recipe endpoint."""
    return override or f"{slug}{TOOL_NAME_SEP}{endpoint}"


def build_tool_parameters(endpoint: EndpointConfig) -> dict[str, Any]:
    """Build the canonical JSON Schema used by every MCP adapter."""
    properties: dict[str, Any] = {
        "page": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "1-based result page.",
        }
    }
    required: list[str] = []
    if endpoint.requires_query:
        properties["q"] = {
            "type": "string",
            "description": "The search query or prompt.",
        }
        required.append("q")
    else:
        properties["q"] = {
            "type": "string",
            "default": "",
            "description": "Optional search query or prompt.",
        }

    for name, config in endpoint.params.items():
        prop: dict[str, Any] = {"type": config.type}
        if config.description:
            prop["description"] = config.description
        if config.example is not None:
            prop["examples"] = [config.example]
        for field in ("enum", "minimum", "maximum", "pattern"):
            value = getattr(config, field)
            if value is not None:
                prop[field] = value
        if config.min_length is not None:
            prop["minLength"] = config.min_length
        if config.max_length is not None:
            prop["maxLength"] = config.max_length
        properties[name] = prop
        if config.required:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def tool_specs_from_registry(registry: Any) -> list[ToolSpec]:
    """Build canonical tool specifications from the live recipe registry."""
    specs: list[ToolSpec] = []
    for recipe in registry.list_all():
        slug = recipe.config.slug
        site_name = recipe.config.name
        for endpoint_name, endpoint in recipe.config.endpoints.items():
            description = endpoint.description or f"{site_name} — {endpoint_name}"
            specs.append(
                ToolSpec(
                    name=build_tool_name(slug, endpoint_name, endpoint.tool_name),
                    description=f"[{site_name}] {description}",
                    slug=slug,
                    endpoint=endpoint_name,
                    parameters=build_tool_parameters(endpoint),
                    endpoint_config=endpoint,
                )
            )
    return specs


def resolve_tool_spec(registry: Any, tool_name: str) -> ToolSpec | None:
    """Resolve canonical and legacy tool names to a specification."""
    specs = tool_specs_from_registry(registry)
    for spec in specs:
        if spec.name == tool_name:
            return spec
    for spec in specs:
        if tool_name == f"{spec.slug}_{spec.endpoint}":
            return spec
    return None


def normalize_tool_arguments(arguments: dict[str, Any]) -> tuple[int, str | None, dict[str, str]]:
    """Normalize common MCP arguments into endpoint query parameters."""
    body = dict(arguments)
    q_value = body.pop("q", None)
    query_alias = body.pop("query", None)
    if q_value is not None and not isinstance(q_value, str):
        raise ValueError("q must be a string")
    if query_alias is not None and not isinstance(query_alias, str):
        raise ValueError("query must be a string")
    q_supplied = q_value is not None and q_value != ""
    alias_supplied = query_alias is not None and query_alias != ""
    if q_supplied and alias_supplied and q_value != query_alias:
        raise ValueError("q and query must not contain conflicting values")
    query = q_value if q_supplied else query_alias

    raw_page = body.pop("page", 1)
    try:
        page = int(raw_page)
        if page < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("page must be an integer >= 1") from None

    params = {"page": str(page)}
    if query is not None:
        params["q"] = str(query)
    for name, value in body.items():
        if value is not None and value != "":
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"{name} must be a scalar value")
            params[name] = str(value)
    return page, str(query) if query is not None else None, params


async def invoke_tool_spec(app: Any, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
    """Execute a canonical tool specification through shared endpoint logic."""
    registry = app.state.registry
    recipe = registry.get(spec.slug)
    if recipe is None:
        raise LookupError(f"recipe '{spec.slug}' was not found")
    page, query, params = normalize_tool_arguments(arguments)
    from web2api.execution import execute_recipe_endpoint

    return await execute_recipe_endpoint(
        app=app,
        recipe=recipe,
        endpoint_name=spec.endpoint,
        page=page,
        q=query,
        query_params=params,
    )


def tool_result_value(data: dict[str, Any]) -> Any:
    """Return the most useful structured result for an MCP HTTP response."""
    error = data.get("error")
    if error:
        return f"Error: {error.get('message', 'unknown error')}"
    items = data.get("items", [])
    if not items:
        return "No results found."
    if len(items) == 1:
        fields = items[0].get("fields", {})
        for key in ("response", "answer", "text", "content", "result"):
            if key in fields:
                return fields[key]
        return fields or items[0]

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
    return simplified


def format_tool_result(data: dict[str, Any]) -> str:
    """Format a web2api response as text for protocol MCP consumers."""
    value = tool_result_value(data)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        results = []
        for item in value:
            if isinstance(item, dict):
                results.append("\n".join(f"{key}: {entry}" for key, entry in item.items()))
            else:
                results.append(str(item))
        return "\n\n---\n\n".join(results)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {entry}" for key, entry in value.items())
    return str(value)
