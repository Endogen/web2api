"""Shared utilities for MCP bridge and protocol server."""

from __future__ import annotations

from typing import Any

TOOL_NAME_SEP = "__"


def build_tool_name(slug: str, endpoint: str) -> str:
    """Build a tool name from recipe slug and endpoint name."""
    return f"{slug}{TOOL_NAME_SEP}{endpoint}"


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """Parse a tool name into (slug, endpoint). Returns None if invalid."""
    parts = name.split(TOOL_NAME_SEP, 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def format_tool_result(data: dict[str, Any]) -> str:
    """Format a web2api JSON response into readable text for MCP consumers."""
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

        # For single-field responses (like AI chat), return the main field
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


def sites_from_registry(registry: Any) -> list[dict[str, Any]]:
    """Extract site/endpoint data from a RecipeRegistry instance."""
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
            "base_url": cfg.base_url,
            "endpoints": endpoints,
        })
    return sites
