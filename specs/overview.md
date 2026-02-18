# Web2API — Overview

## Goal

A server-hosted platform that turns any website into a REST API by scraping target sites live on each request using Playwright. Each "recipe" is a self-contained folder defining how to scrape a specific website. Adding a new folder = new API endpoint, no core code changes required.

## Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI (async)
- **Scraping:** Playwright (async, Chromium)
- **Config:** YAML for declarative recipe definitions
- **Validation:** Pydantic v2 for schemas and config parsing
- **Testing:** pytest + pytest-asyncio + httpx (async test client)
- **Containerization:** Docker (Python + Playwright + Chromium)

## Success Criteria

- [ ] Recipe plugin system: drop a folder → API endpoint appears automatically
- [ ] Declarative YAML recipe format with selectors, actions, field mappings, pagination
- [ ] Optional `scraper.py` escape hatch per recipe for complex scraping logic
- [ ] Unified REST API with `/{site}/read` and `/{site}/search` endpoints
- [ ] Consistent JSON response schema across all recipes (items, pagination, metadata, errors)
- [ ] Browser pool for concurrent request handling (no one-browser-per-request)
- [ ] Hacker News reference recipe implementing both `read` and `search`
- [ ] Project website / index page listing all available APIs with docs
- [ ] Unit/integration test coverage ≥ 80%
- [ ] Working Docker container (Python + Playwright + Chromium)
- [ ] E2E tests that spin up the container, hit the HN API, and validate real responses against the unified schema

## Non-Goals

- No AI/LLM involvement in scraping
- No third-party scraping APIs or services
- No database or persistent storage (stateless scraping on each request)
- No authentication for the API itself (public endpoints)
- No frontend framework — the index page is server-rendered HTML (Jinja2 or similar)
