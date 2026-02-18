"""Recipe discovery and optional scraper plugin loading."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import util
from pathlib import Path

import yaml

from web2api.config import RecipeConfig
from web2api.scraper import BaseScraper


@dataclass(slots=True)
class Recipe:
    """A discovered recipe with validated config and optional scraper."""

    config: RecipeConfig
    scraper: BaseScraper | None
    path: Path


class RecipeRegistry:
    """Registry of recipe plugins discovered from the filesystem."""

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}

    def discover(self, recipes_dir: Path) -> None:
        """Scan ``recipes_dir`` and register discovered recipes."""
        self._recipes.clear()
        if not recipes_dir.exists():
            return

        for recipe_dir in sorted(path for path in recipes_dir.iterdir() if path.is_dir()):
            recipe = self._load_recipe(recipe_dir)
            if recipe is None:
                continue
            self._recipes[recipe.config.slug] = recipe

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

        recipe_data = yaml.safe_load(recipe_config_path.read_text(encoding="utf-8")) or {}
        config = RecipeConfig.model_validate(recipe_data)
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

        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)

        scraper_cls = getattr(module, "Scraper", None)
        if scraper_cls is None:
            return None

        scraper = scraper_cls()
        if not isinstance(scraper, BaseScraper):
            raise TypeError(f"{scraper_path} Scraper must subclass BaseScraper")
        return scraper
