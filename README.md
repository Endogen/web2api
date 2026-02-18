# Web2API

Turn any website into a REST API by scraping it live with Playwright.

Web2API loads recipe folders from `recipes/` at startup. Each recipe defines selectors, actions, fields, and pagination in YAML. Adding a new folder adds a new API site.

## Features

- FastAPI service with async Playwright scraping
- Recipe plugin discovery (`recipes/<slug>/recipe.yaml`)
- Unified JSON schema for `read` and `search` responses
- Optional per-recipe Python override (`scraper.py`) for complex logic
- Shared browser/context pool for concurrent requests
- Docker and docker-compose support

## Quickstart (Docker)

### Prerequisites

- Docker
- Docker Compose (`docker compose`)

### Run

```bash
docker compose up --build
```

Service URL: `http://localhost:8000`

### Verify

```bash
curl -s http://localhost:8000/health | jq
curl -s http://localhost:8000/api/sites | jq
curl -s "http://localhost:8000/hackernews/read?page=1" | jq
curl -s "http://localhost:8000/hackernews/search?q=python&page=1" | jq
```

## Local Development

```bash
source .venv/bin/activate
uvicorn web2api.main:app --reload --port 8000
```

## API Documentation

### Endpoints

- `GET /` HTML index listing discovered sites and links
- `GET /health` Service health
- `GET /api/sites` JSON list of discovered recipes
- `GET /{site}/read?page=1` Scrape page content for a site
- `GET /{site}/search?q=<query>&page=1` Scrape search results for a site

### Common Error Behavior

- Unknown site slug: HTTP `404`
- Search without `q`: HTTP `400`, error code `INVALID_PARAMS`
- Unsupported capability on a recipe: HTTP `400`, error code `CAPABILITY_NOT_SUPPORTED`
- Upstream/browser failure: HTTP `502`, error code `SCRAPE_FAILED`
- Timeout: HTTP `504`, error code `SCRAPE_TIMEOUT`

### Response Shape

All `read`/`search` endpoints return:

```json
{
  "site": {"name": "Hacker News", "slug": "hackernews", "url": "https://news.ycombinator.com"},
  "endpoint": "read",
  "query": null,
  "items": [
    {
      "title": "Example title",
      "url": "https://example.com",
      "fields": {
        "score": 153,
        "author": "pg"
      }
    }
  ],
  "pagination": {
    "current_page": 1,
    "has_next": true,
    "has_prev": false,
    "total_pages": null,
    "total_items": null
  },
  "metadata": {
    "scraped_at": "2026-02-18T12:34:56Z",
    "response_time_ms": 1832,
    "item_count": 30,
    "cached": false
  },
  "error": null
}
```

## Recipe Authoring Guide

### Recipe Layout

```text
recipes/
  <slug>/
    recipe.yaml          # required
    scraper.py           # optional
    README.md            # optional
```

Rules:

- Folder name must match `slug`
- Restart the server to discover new or changed recipes
- Invalid recipes are skipped with warning logs

### Minimal `recipe.yaml`

```yaml
name: "Example Site"
slug: "examplesite"
base_url: "https://example.com"
description: "Example read and search API"
capabilities:
  - read
  - search
endpoints:
  read:
    url: "https://example.com/list?page={page}"
    actions:
      - type: wait
        selector: ".item"
        timeout: 10000
    items:
      container: ".item"
      fields:
        title:
          selector: "a.title"
          attribute: "text"
        url:
          selector: "a.title"
          attribute: "href"
          transform: "absolute_url"
    pagination:
      type: "page_param"
      param: "page"
      start: 1

  search:
    url: "https://example.com/search?q={query}&page={page_zero}"
    actions: []
    items:
      container: ".result"
      fields:
        title:
          selector: "a"
          attribute: "text"
    pagination:
      type: "page_param"
      param: "page"
      start: 0
```

### Supported Actions

- `wait` (`selector`, optional `timeout`)
- `click` (`selector`)
- `scroll` (`direction`, `amount`)
- `type` (`selector`, `text`)
- `sleep` (`ms`)
- `evaluate` (`script`)

### Supported Transforms

- `strip`
- `strip_html`
- `regex_int`
- `regex_float`
- `iso_date`
- `absolute_url`

### Field Context

- `self` (default)
- `next_sibling`
- `parent`

### Optional Custom Scraper

Use `scraper.py` when declarative YAML is not enough:

```python
from playwright.async_api import Page

from web2api.scraper import BaseScraper, ScrapeResult


class Scraper(BaseScraper):
    async def read(self, page: Page, params: dict) -> ScrapeResult:
        # Custom scraping logic
        return ScrapeResult(items=[], current_page=1, has_next=False, has_prev=False)
```

If custom methods are not implemented, Web2API falls back to declarative YAML for that endpoint.

## Reference Recipe

- `recipes/hackernews/recipe.yaml`
- `recipes/hackernews/README.md`

Use this recipe as a template for new sites.

## Testing

```bash
source .venv/bin/activate
ruff check . --fix && ruff format .
pytest tests/unit tests/integration --timeout=30 -x -q
```
