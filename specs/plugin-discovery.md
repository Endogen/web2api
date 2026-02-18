# Plugin Discovery Specification

## Overview

The system automatically discovers recipe folders and registers API routes at startup. No manual registration, no config file listing recipes — just drop a folder and it works.

## Discovery Process

### At Startup

1. Scan the `recipes/` directory for subdirectories
2. For each subdirectory, look for `recipe.yaml`
3. Parse and validate the YAML config with Pydantic
4. If `scraper.py` exists, import the `Scraper` class
5. Register `GET /{slug}/read` and/or `GET /{slug}/search` routes based on declared capabilities
6. Log discovered recipes and any errors (invalid YAML, missing required fields, etc.)

### Route Registration

```python
# Pseudo-code for dynamic route registration
for recipe in discovered_recipes:
    if "read" in recipe.capabilities:
        app.add_api_route(
            f"/{recipe.slug}/read",
            create_read_handler(recipe),
            methods=["GET"]
        )
    if "search" in recipe.capabilities:
        app.add_api_route(
            f"/{recipe.slug}/search",
            create_search_handler(recipe),
            methods=["GET"]
        )
```

### Validation

On discovery, validate each recipe:
- `recipe.yaml` must parse as valid YAML
- Must pass Pydantic schema validation (RecipeConfig model)
- `slug` in YAML must match the folder name
- Declared capabilities must have corresponding endpoint definitions
- If `scraper.py` exists, it must define a `Scraper` class with the expected interface
- Selectors are strings (not validated until scraping time)

Invalid recipes are logged as warnings and skipped — they don't prevent the server from starting.

### Recipe Registry

A central `RecipeRegistry` holds all validated recipes:

```python
class RecipeRegistry:
    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}

    def discover(self, recipes_dir: Path) -> None:
        """Scan directory and load all valid recipes."""

    def get(self, slug: str) -> Recipe | None:
        """Get a recipe by slug."""

    def list_all(self) -> list[Recipe]:
        """List all registered recipes."""

    @property
    def count(self) -> int:
        """Number of registered recipes."""
```

### Recipe Object

A loaded `Recipe` combines the parsed YAML config with the optional custom scraper:

```python
@dataclass
class Recipe:
    config: RecipeConfig        # Parsed from recipe.yaml
    scraper: BaseScraper | None # Loaded from scraper.py, or None
    path: Path                  # Filesystem path to recipe folder
```

## File Watching (Future / Out of Scope for v1)

v1 requires a server restart to pick up new recipes. Future enhancement: use `watchdog` to detect new/changed recipe folders and hot-reload routes.
