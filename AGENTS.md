# AGENTS.md

## Project

Web2API — a platform that turns any website into a REST API by scraping target sites live on each request using Playwright. Each website-API is a self-contained "recipe" folder with declarative YAML config and optional Python escape hatch. Drop a folder = new API endpoint.

## Tech Stack
- **Language:** Python 3.12
- **Framework:** FastAPI (async)
- **Scraping:** Playwright (async, Chromium)
- **Config:** YAML (PyYAML) + Pydantic v2 for validation
- **Testing:** pytest + pytest-asyncio + httpx + pytest-cov
- **Templates:** Jinja2 (index page)
- **Container:** Docker (Python 3.12-slim + Playwright + Chromium)

## Commands
- **Install:** `pip install -e ".[dev]"`
- **Run:** `uvicorn web2api.main:app --reload --port 8000`
- **Test (unit + integration):** `pytest tests/unit tests/integration --timeout=30 -v`
- **Test (E2E, requires Docker):** `pytest tests/e2e -v`
- **Coverage:** `pytest tests/unit tests/integration --cov=web2api --cov-report=term-missing --timeout=30`
- **Lint:** `ruff check . --fix`
- **Format:** `ruff format .`

## Backpressure
Run these after EACH implementation (in order):
1. `ruff check . --fix && ruff format .`
2. `pytest tests/unit tests/integration --timeout=30 -x -q` (stop on first failure)

## Project Structure
```
web2api/
├── web2api/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, route registration
│   ├── config.py            # Pydantic models for recipe config
│   ├── schemas.py           # Pydantic models for API response
│   ├── pool.py              # BrowserPool
│   ├── engine.py            # Scraping engine (actions, extraction, transforms)
│   ├── registry.py          # RecipeRegistry (plugin discovery)
│   ├── scraper.py           # BaseScraper abstract class
│   └── templates/
│       └── index.html       # Jinja2 index page
├── recipes/
│   └── hackernews/
│       ├── recipe.yaml
│       └── README.md
├── tests/
│   ├── unit/
│   ├── integration/
│   │   └── fixtures/
│   └── e2e/
├── specs/                   # Requirement specs (read-only reference)
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Conventions
- All async where possible (async Playwright, async FastAPI handlers)
- Pydantic v2 for all data validation (model_validate, not parse_obj)
- Type hints on all function signatures
- Docstrings on all public classes and functions
- Conventional commit messages: `feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`
- Tests mirror source structure: `web2api/pool.py` → `tests/unit/test_pool.py` or `tests/integration/test_pool.py`
- Keep `specs/` read-only — they are the source of truth for requirements

## Human Decisions
<!-- Record decisions made by humans here so future iterations have context -->

## Learnings
<!-- Agent appends operational notes here during execution -->
