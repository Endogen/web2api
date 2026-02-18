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

## Environment
A pre-provisioned venv exists at `.venv/` with all dependencies installed (fastapi, playwright, pytest, ruff, etc.).
Always activate it before running commands:
```bash
source .venv/bin/activate
```

## Commands
- **Install:** `source .venv/bin/activate && pip install -e ".[dev]"`
- **Run:** `source .venv/bin/activate && uvicorn web2api.main:app --reload --port 8000`
- **Test (unit + integration):** `source .venv/bin/activate && pytest tests/unit tests/integration --timeout=30 -v`
- **Test (E2E, requires Docker):** `source .venv/bin/activate && pytest tests/e2e -v`
- **Coverage:** `source .venv/bin/activate && pytest tests/unit tests/integration --cov=web2api --cov-report=term-missing --timeout=30`
- **Lint:** `source .venv/bin/activate && ruff check . --fix`
- **Format:** `source .venv/bin/activate && ruff format .`

## Backpressure
Run these after EACH implementation (in order):
1. `source .venv/bin/activate && ruff check . --fix && ruff format .`
2. `source .venv/bin/activate && pytest tests/unit tests/integration --timeout=30 -x -q` (stop on first failure)

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
- 2026-02-18: This sandbox does not have `ruff`/`pytest` preinstalled and cannot reach PyPI, so backpressure commands require a pre-provisioned toolchain or offline package mirror.
- 2026-02-18: `pytest-timeout` is unavailable in the pre-provisioned environment and network access is blocked, so `pytest ... --timeout=30` fails unless an offline wheel/mirror is provided.
- 2026-02-18: Playwright Chromium launch fails in this sandbox (`sandbox_host_linux.cc` fatal), so real browser integration tests must use fakes/mocks here.
- 2026-02-18: Backpressure commands succeeded in this environment (`ruff` available and `pytest ... --timeout=30` works inside `.venv`), indicating the toolchain is provisioned for local unit/integration runs.
- 2026-02-18: Live Hacker News integration coverage is implemented in `tests/integration/test_hackernews_live.py`, gated by `WEB2API_RUN_LIVE_HN_TESTS=1` and auto-skipping when DNS/browser sandbox errors make real-site access unavailable.
- 2026-02-18: Current backpressure baseline is fast in this repo state (`pytest tests/unit tests/integration --timeout=30 -x -q` completed with `30 passed, 1 skipped` in under a second).
