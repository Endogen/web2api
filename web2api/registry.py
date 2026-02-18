"""Recipe discovery and optional scraper plugin loading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import util
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import yaml

from web2api.config import RecipeConfig, parse_recipe_config
from web2api.logging_utils import log_event
from web2api.scraper import BaseScraper

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Recipe:
    """A discovered recipe with validated config and optional scraper."""

    config: RecipeConfig
    scraper: BaseScraper | None
    path: Path


class RecipeRegistry:
    """Registry of recipe plugins discovered from the filesystem."""

    def __init__(self) -> None:
        """Initialize an empty in-memory recipe registry."""
        self._recipes: dict[str, Recipe] = {}

    def discover(self, recipes_dir: Path) -> None:
        """Scan ``recipes_dir`` and register discovered recipes."""
        self._recipes.clear()
        if not recipes_dir.exists() or not recipes_dir.is_dir():
            log_event(
                logger,
                logging.WARNING,
                "registry.discover_skipped",
                recipes_dir=str(recipes_dir),
                reason="missing_or_not_directory",
            )
            return

        log_event(logger, logging.INFO, "registry.discover_started", recipes_dir=str(recipes_dir))
        for recipe_dir in sorted(path for path in recipes_dir.iterdir() if path.is_dir()):
            try:
                recipe = self._load_recipe(recipe_dir)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    logging.WARNING,
                    "registry.recipe_invalid",
                    recipe_dir=recipe_dir.name,
                    error=str(exc),
                )
                logger.warning("Skipping invalid recipe '%s': %s", recipe_dir.name, exc)
                continue

            if recipe is None:
                continue

            slug = recipe.config.slug
            if slug in self._recipes:
                log_event(
                    logger,
                    logging.WARNING,
                    "registry.recipe_duplicate_slug",
                    recipe_dir=recipe_dir.name,
                    slug=slug,
                )
                logger.warning(
                    "Skipping recipe '%s': duplicate slug '%s'",
                    recipe_dir.name,
                    slug,
                )
                continue
            self._recipes[slug] = recipe
            log_event(
                logger,
                logging.INFO,
                "registry.recipe_loaded",
                slug=slug,
                has_custom_scraper=recipe.scraper is not None,
            )
        log_event(
            logger,
            logging.INFO,
            "registry.discover_completed",
            recipe_count=len(self._recipes),
        )

    def get(self, slug: str) -> Recipe | None:
        """Get a discovered recipe by slug."""
        return self._recipes.get(slug)

    def list_all(self) -> list[Recipe]:
        """List all discovered recipes."""
        return list(self._recipes.values())

    @property
    def count(self) -> int:
        """Return the number of discovered recipes."""
        return len(self._recipes)

    def _load_recipe(self, recipe_dir: Path) -> Recipe | None:
        recipe_config_path = recipe_dir / "recipe.yaml"
        if not recipe_config_path.exists():
            return None

        raw_data = yaml.safe_load(recipe_config_path.read_text(encoding="utf-8"))
        if raw_data is None:
            raise ValueError(f"{recipe_config_path} is empty")
        if not isinstance(raw_data, dict):
            raise ValueError(f"{recipe_config_path} must contain a YAML mapping")

        recipe_data = {str(key): value for key, value in raw_data.items()}
        config = parse_recipe_config(recipe_data, folder_name=recipe_dir.name)
        scraper = self._load_scraper(recipe_dir)
        return Recipe(config=config, scraper=scraper, path=recipe_dir)

    def _load_scraper(self, recipe_dir: Path) -> BaseScraper | None:
        scraper_path = recipe_dir / "scraper.py"
        if not scraper_path.exists():
            return None

        module_name = f"_web2api_recipe_{recipe_dir.name}"
        spec = util.spec_from_file_location(module_name, scraper_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"failed to load scraper module from {scraper_path}")

        module = self._load_module(spec)
        spec.loader.exec_module(module)

        scraper_cls = getattr(module, "Scraper", None)
        if scraper_cls is None:
            raise ValueError(f"{scraper_path} must define a Scraper class")

        scraper = scraper_cls()
        if not isinstance(scraper, BaseScraper):
            raise TypeError(f"{scraper_path} Scraper must subclass BaseScraper")
        return scraper

    @staticmethod
    def _load_module(spec: ModuleSpec) -> ModuleType:
        """Create a module instance for a recipe scraper spec."""
        module = util.module_from_spec(spec)
        if not isinstance(module, ModuleType):
            raise ImportError("failed to create module object for scraper")
        return module
