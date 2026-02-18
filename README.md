# Web2API

Turn any website into a REST API by scraping it live with Playwright.

Web2API loads recipe folders from `recipes/` at startup. Each recipe defines endpoints with selectors, actions, fields, and pagination in YAML. Optional Python scrapers handle interactive or complex sites. Drop a folder — get an API.

## Features

- **Arbitrary named endpoints** — recipes define as many endpoints as needed (not limited to read/search)
- **Declarative YAML recipes** with selectors, actions, transforms, and pagination
- **Custom Python scrapers** for interactive sites (e.g. typing text, waiting for dynamic content)
- **Shared browser/context pool** for concurrent Playwright requests
- **Unified JSON response schema** across all recipes and endpoints
- **Docker deployment** with auto-restart

## Quickstart (Docker)

```bash
docker compose up --build -d
```

Service: `http://localhost:8010`

### Verify

```bash
curl -s http://localhost:8010/health | jq
curl -s http://localhost:8010/api/sites | jq
```

## Included Recipes

### Hacker News (`hackernews`)

```bash
# Front page stories
curl -s "http://localhost:8010/hackernews/read?page=1" | jq

# Search via Algolia
curl -s "http://localhost:8010/hackernews/search?q=python&page=1" | jq
```

### DeepL Translator (`deepl`)

```bash
# German → English
curl -s "http://localhost:8010/deepl/de-en?q=Hallo%20Welt" | jq

# English → German
curl -s "http://localhost:8010/deepl/en-de?q=Hello%20world" | jq
```

## API

### Discovery

| Endpoint | Description |
|---|---|
| `GET /` | HTML index listing all recipes and endpoints |
| `GET /health` | Service and browser pool health |
| `GET /api/sites` | JSON list of all recipes with endpoint metadata |

### Recipe Endpoints

All recipe endpoints follow the pattern: `GET /{slug}/{endpoint}?page=1&q=...`

- `page` — pagination (default: 1)
- `q` — query text (required when `requires_query: true`)

### Error Codes

| HTTP | Code | When |
|---|---|---|
| 400 | `INVALID_PARAMS` | Missing required `q` parameter |
| 400 | `CAPABILITY_NOT_SUPPORTED` | Endpoint not defined for recipe |
| 404 | — | Unknown recipe or endpoint |
| 502 | `SCRAPE_FAILED` | Browser/upstream failure |
| 504 | `SCRAPE_TIMEOUT` | Scrape exceeded timeout |

### Response Shape

```json
{
  "site": { "name": "...", "slug": "...", "url": "..." },
  "endpoint": "read",
  "query": null,
  "items": [
    {
      "title": "Example title",
      "url": "https://example.com",
      "fields": { "score": 153, "author": "pg" }
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

## Recipe Authoring

### Layout

```
recipes/
  <slug>/
    recipe.yaml     # required — endpoint definitions
    scraper.py      # optional — custom Python scraper
    README.md       # optional — documentation
```

- Folder name must match `slug`
- Restart the service to pick up new or changed recipes
- Invalid recipes are skipped with warning logs

### Example: Declarative Endpoints

```yaml
name: "Example Site"
slug: "examplesite"
base_url: "https://example.com"
description: "Scrapes example.com listings and search"
endpoints:
  read:
    description: "Browse listings"
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
    description: "Search listings"
    requires_query: true
    url: "https://example.com/search?q={query}&page={page_zero}"
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

### Endpoint Config Fields

| Field | Required | Description |
|---|---|---|
| `url` | yes | URL template with `{page}`, `{page_zero}`, `{query}` placeholders |
| `description` | no | Human-readable endpoint description |
| `requires_query` | no | If `true`, the `q` parameter is mandatory (default: `false`) |
| `actions` | no | Playwright actions to run before extraction |
| `items` | yes | Container selector + field definitions |
| `pagination` | yes | Pagination strategy (`page_param`, `offset_param`, or `next_link`) |

### Actions

| Type | Parameters |
|---|---|
| `wait` | `selector`, `timeout` (optional) |
| `click` | `selector` |
| `scroll` | `direction` (down/up), `amount` (pixels or "bottom") |
| `type` | `selector`, `text` |
| `sleep` | `ms` |
| `evaluate` | `script` |

### Transforms

`strip` · `strip_html` · `regex_int` · `regex_float` · `iso_date` · `absolute_url`

### Field Context

`self` (default) · `next_sibling` · `parent`

### Custom Scraper

For interactive or complex sites, add a `scraper.py` with a `Scraper` class:

```python
from playwright.async_api import Page
from web2api.scraper import BaseScraper, ScrapeResult


class Scraper(BaseScraper):
    def supports(self, endpoint: str) -> bool:
        return endpoint in {"de-en", "en-de"}

    async def scrape(self, endpoint: str, page: Page, params: dict) -> ScrapeResult:
        # page is BLANK — navigate yourself
        await page.goto("https://example.com")
        # ... interact with the page ...
        return ScrapeResult(
            items=[{"title": "result", "fields": {"key": "value"}}],
            current_page=params["page"],
            has_next=False,
        )
```

- `supports(endpoint)` — declare which endpoints use custom scraping
- `scrape(endpoint, page, params)` — `page` is blank, you must `goto()` yourself
- `params` contains `page` (int) and `query` (str | None)
- Endpoints not handled by the scraper fall back to declarative YAML

## Configuration

Environment variables (with defaults):

| Variable | Default | Description |
|---|---|---|
| `POOL_MAX_CONTEXTS` | 5 | Max browser contexts in pool |
| `POOL_CONTEXT_TTL` | 50 | Requests per context before recycling |
| `POOL_ACQUIRE_TIMEOUT` | 30 | Seconds to wait for a context |
| `POOL_PAGE_TIMEOUT` | 15000 | Page navigation timeout (ms) |
| `POOL_QUEUE_SIZE` | 20 | Max queued requests |
| `SCRAPE_TIMEOUT` | 30 | Overall scrape timeout (seconds) |
| `RECIPES_DIR` | `./recipes` | Path to recipes directory |
| `LOG_LEVEL` | `info` | Log level |

## Testing

```bash
# Inside the container or with deps installed:
pytest tests/unit tests/integration --timeout=30 -x -q
```

## Tech Stack

- Python 3.12 + FastAPI + Playwright (Chromium)
- Pydantic for config validation
- Docker for deployment

## License

MIT
