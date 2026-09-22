"""Canonical loading and validation for recipe configuration and code."""

from __future__ import annotations

import ast
import logging
from importlib import util
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from web2api.config import RecipeConfig, parse_recipe_config
from web2api.plugin import PluginConfig, parse_plugin_config
from web2api.scraper import BaseScraper

logger = logging.getLogger(__name__)


def _load_mapping(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise ValueError(f"missing {path.name} in {path.parent}")
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return {str(key): value for key, value in raw.items()}


def load_recipe_config(
    recipe_dir: Path,
    *,
    require_matching_folder: bool = True,
) -> RecipeConfig:
    """Load ``recipe.yaml`` and optionally require its slug to match the folder."""
    data = _load_mapping(recipe_dir / "recipe.yaml", required=True)
    assert data is not None
    folder_name = recipe_dir.name if require_matching_folder else None
    return parse_recipe_config(data, folder_name=folder_name)


def load_plugin_config(recipe_dir: Path) -> PluginConfig | None:
    """Load optional ``plugin.yaml`` from a recipe directory."""
    data = _load_mapping(recipe_dir / "plugin.yaml", required=False)
    return None if data is None else parse_plugin_config(data)


def _load_module(spec: ModuleSpec) -> ModuleType:
    module = util.module_from_spec(spec)
    if not isinstance(module, ModuleType):
        raise ImportError("failed to create module object for scraper")
    return module


def load_scraper(recipe_dir: Path, *, trusted: bool) -> BaseScraper | None:
    """Load a trusted custom scraper, or safely ignore an untrusted one."""
    scraper_path = recipe_dir / "scraper.py"
    if not scraper_path.exists():
        return None
    if not trusted:
        logger.warning("Skipping custom scraper for untrusted recipe '%s'", recipe_dir.name)
        return None
    module_name = f"_web2api_recipe_{recipe_dir.name}_{abs(hash(scraper_path))}"
    spec = util.spec_from_file_location(module_name, scraper_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"failed to load scraper module from {scraper_path}")
    module = _load_module(spec)
    spec.loader.exec_module(module)
    scraper_class = getattr(module, "Scraper", None)
    if scraper_class is None:
        raise ValueError(f"{scraper_path} must define a Scraper class")
    scraper = scraper_class()
    if not isinstance(scraper, BaseScraper):
        raise TypeError(f"{scraper_path} Scraper must subclass BaseScraper")
    return scraper


def validate_scraper_source(recipe_dir: Path) -> None:
    """Validate scraper structure without importing or executing recipe code."""
    scraper_path = recipe_dir / "scraper.py"
    if not scraper_path.exists():
        return
    try:
        tree = ast.parse(scraper_path.read_text(encoding="utf-8"), filename=str(scraper_path))
    except SyntaxError as exc:
        raise ValueError(f"invalid scraper syntax in {scraper_path}: {exc}") from exc
    if not any(isinstance(node, ast.ClassDef) and node.name == "Scraper" for node in tree.body):
        raise ValueError(f"{scraper_path} must define a Scraper class")
