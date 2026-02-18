# Implementation Plan

STATUS: PLANNING_COMPLETE

## Phase 1: Project Skeleton & Core Models

- [x] 1.1 — Set up Python project structure: `pyproject.toml` with dependencies (fastapi, uvicorn, playwright, pyyaml, pydantic), `web2api/` package with `__init__.py`, `main.py` entry point
- [ ] 1.2 — Define Pydantic models for recipe config: `RecipeConfig`, `EndpointConfig`, `ItemsConfig`, `FieldConfig`, `ActionConfig`, `PaginationConfig` (from `specs/recipe-format.md`)
- [ ] 1.3 — Define Pydantic models for unified API response: `SiteInfo`, `ItemResponse`, `PaginationResponse`, `MetadataResponse`, `ErrorResponse`, `ApiResponse` (from `specs/api-schema.md`)
- [ ] 1.4 — Define `BaseScraper` abstract class with `read()`, `search()`, `supports_read()`, `supports_search()` methods and `ScrapeResult` dataclass
- [ ] 1.5 — Write unit tests for config model validation: valid configs parse, invalid configs raise errors, defaults applied, slug matching

## Phase 2: Recipe Discovery & Plugin System

- [ ] 2.1 — Implement `RecipeRegistry`: scan `recipes/` dir, parse `recipe.yaml` files with Pydantic, load optional `scraper.py` modules, store as `Recipe` objects
- [ ] 2.2 — Implement validation logic: slug-folder match check, capability-endpoint consistency, duplicate slug detection (warn + skip), graceful handling of invalid recipes
- [ ] 2.3 — Write integration tests for discovery: valid recipe loaded, invalid recipe skipped, empty dir handled, duplicate slugs warned, custom scraper loaded

## Phase 3: Browser Pool

- [ ] 3.1 — Implement `BrowserPool`: launch Chromium, manage context pool with asyncio.Semaphore/Queue, acquire/release with context manager, configurable pool size and timeouts
- [ ] 3.2 — Implement context recycling: track usage count per context, close/recreate after TTL, force-close corrupted contexts
- [ ] 3.3 — Implement health reporting: `pool.health` property returning connection status, context counts, queue size, total requests served
- [ ] 3.4 — Wire pool into FastAPI lifespan: start pool on startup, stop on shutdown
- [ ] 3.5 — Write integration tests for pool: acquire/release works, concurrent requests respect limits, timeout on exhaustion, recycling, health stats

## Phase 4: Scraping Engine

- [ ] 4.1 — Implement URL template builder: substitute `{page}`, `{page_zero}`, `{query}` with proper URL encoding, handle page number mapping per recipe's pagination start value
- [ ] 4.2 — Implement action executor: process action list sequentially (wait, click, scroll, type, sleep, evaluate) with per-action error handling
- [ ] 4.3 — Implement item extractor: query container selector, resolve field elements (self/next_sibling/parent context), extract attributes (text/href/src/custom)
- [ ] 4.4 — Implement field transforms: `regex_int`, `regex_float`, `strip`, `strip_html`, `iso_date`, `absolute_url` — each as a pure function, with fallback to null on failure
- [ ] 4.5 — Implement pagination detection: `page_param` (items found = has_next), `next_link` (selector exists = has_next), `offset_param`
- [ ] 4.6 — Implement custom scraper integration: check if recipe has Scraper class, delegate to it for supported endpoints, fall back to declarative config otherwise
- [ ] 4.7 — Assemble the `scrape()` function: acquire page → build URL → navigate → execute actions → extract items → transform → detect pagination → build unified response → release page
- [ ] 4.8 — Write unit tests for transforms and URL builder
- [ ] 4.9 — Write integration tests for scraping engine: use local HTTP server with HTML fixtures, test extraction, context resolution, optional fields, transforms, actions

## Phase 5: API Routes & Index Page

- [ ] 5.1 — Implement dynamic route registration: on startup, iterate recipes and register `GET /{slug}/read` and `GET /{slug}/search` handlers based on capabilities
- [ ] 5.2 — Implement route handlers: parse query params (page, q), call scraping engine, return unified JSON response, handle errors with proper HTTP status codes
- [ ] 5.3 — Implement `GET /api/sites` endpoint: return JSON list of all recipes with metadata and capabilities
- [ ] 5.4 — Implement `GET /health` endpoint: return server and browser pool health status
- [ ] 5.5 — Implement `GET /` index page: server-rendered HTML (Jinja2) listing all recipes with name, description, capabilities, and "try it" links
- [ ] 5.6 — Write integration tests for API: valid read/search responses, 404 for unknown slug, 400 for unsupported capability, sites listing, health endpoint, index page renders

## Phase 6: Hacker News Reference Recipe

- [ ] 6.1 — Create `recipes/hackernews/recipe.yaml`: metadata, read endpoint (front page stories with selectors for title, URL, score, author, comments, time, ID), search endpoint, pagination config
- [ ] 6.2 — Test the HN recipe manually against the real site (via integration test with live scraping) — adjust selectors as needed until extraction works reliably
- [ ] 6.3 — Write `recipes/hackernews/README.md` documenting the recipe as a template for others

## Phase 7: Docker & E2E

- [ ] 7.1 — Create `Dockerfile`: Python 3.12-slim base, install deps, install Playwright + Chromium, copy app + recipes, expose port 8000
- [ ] 7.2 — Create `docker-compose.yml`: service definition, environment variables, health check
- [ ] 7.3 — Write E2E tests: build container, wait for health, call HN read endpoint (validate schema, non-empty items, pagination), call HN search endpoint (validate schema, query, items), call sites listing (HN present), tear down
- [ ] 7.4 — Verify test coverage ≥ 80% with `pytest-cov`, add any missing tests

## Phase 8: Polish

- [ ] 8.1 — Add `README.md` with project overview, quickstart (Docker), recipe authoring guide, API documentation
- [ ] 8.2 — Add structured logging throughout (request IDs, scrape timing, errors)
- [ ] 8.3 — Review and clean up: type hints complete, docstrings on public interfaces, remove dead code
