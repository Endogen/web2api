"""Recipe discovery and optional metadata loading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web2api.config import RecipeConfig
from web2api.logging_utils import log_event
from web2api.plugin import PluginConfig, evaluate_plugin_status
from web2api.recipe_loader import load_plugin_config, load_recipe_config, load_scraper
from web2api.recipe_manager import entry_is_trusted, get_manifest_record, load_manifest
from web2api.scraper import BaseScraper

logger = logging.getLogger(__name__)
_DISABLED_MARKER = ".disabled"


@dataclass(slots=True)
class Recipe:
    """A discovered recipe with validated config and optional scraper."""

    config: RecipeConfig
    scraper: BaseScraper | None
    path: Path
    plugin: PluginConfig | None = None


class RecipeRegistry:
    """Registry of recipes discovered from the filesystem."""

    def __init__(
        self,
        *,
        app_version: str | None = None,
        enforce_plugin_compatibility: bool = False,
    ) -> None:
        """Initialize an empty in-memory recipe registry."""
        self._recipes: dict[str, Recipe] = {}
        self._app_version = app_version
        self._enforce_plugin_compatibility = enforce_plugin_compatibility

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

        manifest = load_manifest(recipes_dir)
        log_event(logger, logging.INFO, "registry.discover_started", recipes_dir=str(recipes_dir))
        for recipe_dir in sorted(path for path in recipes_dir.iterdir() if path.is_dir()):
            if (recipe_dir / _DISABLED_MARKER).exists():
                log_event(
                    logger,
                    logging.INFO,
                    "registry.recipe_disabled",
                    recipe_dir=recipe_dir.name,
                )
                continue
            try:
                recipe = self._load_recipe(recipe_dir, manifest=manifest)
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

            if recipe.plugin is not None:
                status = evaluate_plugin_status(
                    recipe.plugin,
                    current_web2api_version=self._app_version,
                )
                compatibility = status.get("compatibility", {})
                is_compatible = (
                    compatibility.get("is_compatible")
                    if isinstance(compatibility, dict)
                    else None
                )
                if is_compatible is False:
                    log_event(
                        logger,
                        logging.WARNING,
                        "registry.recipe_incompatible",
                        recipe_dir=recipe_dir.name,
                        slug=recipe.config.slug,
                        app_version=self._app_version,
                        min=compatibility.get("min") if isinstance(compatibility, dict) else None,
                        max=compatibility.get("max") if isinstance(compatibility, dict) else None,
                    )
                    if self._enforce_plugin_compatibility:
                        logger.warning(
                            "Skipping recipe '%s': incompatible plugin web2api version bounds",
                            recipe_dir.name,
                        )
                        continue
                    logger.warning(
                        "Recipe '%s' has incompatible plugin web2api version bounds",
                        recipe_dir.name,
                    )

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

    def _load_recipe(
        self,
        recipe_dir: Path,
        *,
        manifest: dict[str, Any] | None = None,
    ) -> Recipe | None:
        if not (recipe_dir / "recipe.yaml").exists():
            return None
        config = load_recipe_config(recipe_dir)
        manifest_record = get_manifest_record(manifest or {}, config.slug)
        trusted = entry_is_trusted(manifest_record)
        return Recipe(
            config=config,
            scraper=load_scraper(recipe_dir, trusted=trusted),
            path=recipe_dir,
            plugin=load_plugin_config(recipe_dir),
        )
