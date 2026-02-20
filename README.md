# Web2API

Turn any website into a REST API by scraping it live with Playwright.

Web2API loads recipe folders from `recipes/` at startup. Each recipe defines endpoints with selectors, actions, fields, and pagination in YAML. Optional Python scrapers handle interactive or complex sites. Optional plugin metadata can declare external dependencies and required env vars. Drop a folder — get an API.

## Features

- **Arbitrary named endpoints** — recipes define as many endpoints as needed (not limited to read/search)
- **Declarative YAML recipes** with selectors, actions, transforms, and pagination
- **Custom Python scrapers** for interactive sites (e.g. typing text, waiting for dynamic content)
- **Optional plugin metadata** (`plugin.yaml`) for recipe-specific dependency requirements
- **Shared browser/context pool** for concurrent Playwright requests
- **In-memory response cache** with stale-while-revalidate
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

## CLI

Web2API ships with a management CLI:

```bash
web2api --help
```

### Plugin Commands

```bash
# List all recipe folders with plugin readiness
web2api plugins list

# Check missing env vars/commands/packages
web2api plugins doctor
web2api plugins doctor x
web2api plugins doctor x --no-run-healthchecks
web2api plugins doctor x --allow-untrusted

# Install plugin recipe from source
web2api plugins add ./my-recipe
web2api plugins add https://github.com/acme/web2api-recipes.git --ref v1.2.0 --subdir recipes/news

# Update managed plugin from recorded source
web2api plugins update x --yes
web2api plugins update x --ref v1.3.0 --subdir recipes/x --yes

# Install plugin recipe from catalog
web2api plugins catalog list
web2api plugins catalog add hackernews --yes

# Install declared dependencies for a plugin recipe (host)
web2api plugins install x --yes
web2api plugins install x --apt --yes   # include apt packages

# Generate Dockerfile snippet for plugin dependencies
web2api plugins install x --target docker --apt

# Remove plugin recipe + manifest record
web2api plugins uninstall x --yes

# Disable/enable a recipe (writes/removes recipes/<slug>/.disabled)
web2api plugins disable x --yes
web2api plugins enable x
```

`plugins install` does not run `apt` installs unless `--apt` is explicitly passed.
Install-state records are stored in `recipes/.web2api_plugins.json`.
Default catalog path is `plugins/catalog.yaml` in a source checkout, with a bundled fallback
inside the installed package.
`plugins update` works only for plugins tracked in the manifest.

Plugins installed from untrusted sources (for example git URLs) are blocked from executing
install/healthcheck commands unless `--allow-untrusted` is passed.

### Self Update Commands

```bash
# Show current version + recommended update method
web2api self update check

# Apply update using auto-detected method (pip/git/docker)
web2api self update apply --yes

# Pin explicit method or target version/ref
web2api self update apply --method pip --to 0.1.0 --yes
web2api self update apply --method git --to v0.1.0 --yes
```

For `--method git`, `self update apply` checks out a tag:
- if `--to` is provided, that tag/ref is used
- if `--to` is omitted, the latest sortable git tag is used

After `self update apply`, the CLI automatically runs `web2api plugins doctor`.

## Discover Recipes

Recipe availability is dynamic. Use discovery endpoints instead of relying on a static README list.

```bash
# List all discovered sites and endpoint metadata
curl -s "http://localhost:8010/api/sites" | jq

# Print endpoint paths with required params
curl -s "http://localhost:8010/api/sites" | jq -r '
  .[] as $site
  | $site.endpoints[]
  | "/\($site.slug)/\(.name)  params: page" + (if .requires_query then ", q" else "" end)
'

# Print ready-to-run URL templates
curl -s "http://localhost:8010/api/sites" | jq -r '
  .[] as $site
  | $site.endpoints[]
  | "http://localhost:8010/\($site.slug)/\(.name)?"
    + (if .requires_query then "q=<query>&" else "" end)
    + "page=1"
'

# Example call pattern (no query endpoint)
curl -s "http://localhost:8010/{slug}/{endpoint}?page=1" | jq

# Example call pattern (query endpoint)
curl -s "http://localhost:8010/{slug}/{endpoint}?q=hello&page=1" | jq
```

For custom scraper parameters beyond `page` and `q`, check the specific recipe folder
(`recipes/<slug>/scraper.py`).

## API

### Discovery

| Endpoint | Description |
|---|---|
| `GET /` | HTML index listing all recipes and endpoints |
| `GET /health` | Service, browser pool, and cache health |
| `GET /api/sites` | JSON list of all recipes with endpoint metadata |

### Recipe Endpoints

All recipe endpoints follow the pattern: `GET /{slug}/{endpoint}?page=1&q=...`

- `page` — pagination (default: 1)
- `q` — query text (required when `requires_query: true`)
- additional query params are passed to custom scrapers
- extra query param names must match `[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}` and values are capped at 512 chars

### Error Codes

| HTTP | Code | When |
|---|---|---|
| 400 | `INVALID_PARAMS` | Missing required `q` or invalid extra query parameters |
| 404 | — | Unknown recipe or endpoint |
| 502 | `SCRAPE_FAILED` | Browser/upstream failure |
| 504 | `SCRAPE_TIMEOUT` | Scrape exceeded timeout |

### Caching

- Successful responses are cached in-memory by `(slug, endpoint, page, q, extra params)`.
- Cache hits return `metadata.cached: true`.
- Stale entries can be served immediately while a background refresh updates the cache.

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
    plugin.yaml     # optional — dependency metadata and runtime checks
    README.md       # optional — documentation
```

- Folder name must match `slug`
- `slug` cannot be a reserved system route (`api`, `health`, `docs`, `openapi`, `redoc`)
- Recipe folders containing `.disabled` are skipped by discovery
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

Pagination notes:
`{page}` resolves to `start + ((api_page - 1) * step)`.

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
- `params` always contains `page` (int) and `query` (str | None)
- `params` also includes validated extra query params (for example `count`)
- Endpoints not handled by the scraper fall back to declarative YAML

### Plugin Metadata (Optional)

Use `plugin.yaml` to declare install/runtime requirements for a recipe:

```yaml
version: "1.0.0"
web2api:
  min: "0.2.0"
  max: "1.0.0"
requires_env:
  - BIRD_AUTH_TOKEN
  - BIRD_CT0
dependencies:
  commands:
    - bird
  python:
    - httpx
  apt:
    - nodejs
  npm:
    - "@steipete/bird"
healthcheck:
  command: ["bird", "--version"]
```

Version bounds in `web2api.min` / `web2api.max` use numeric `major.minor.patch` format.

`GET /api/sites` now includes a `plugin` block (or `null`) with:

- declared metadata from `plugin.yaml`
- computed `status.ready` plus missing env vars/commands/python packages
- unverified package declarations (`apt`, `npm`) for operators

Compatibility enforcement:
- `PLUGIN_ENFORCE_COMPATIBILITY=false` (default): incompatible plugins are loaded but reported as not ready.
- `PLUGIN_ENFORCE_COMPATIBILITY=true`: incompatible plugins are skipped at discovery time.

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
| `CACHE_ENABLED` | true | Enable in-memory response caching |
| `CACHE_TTL_SECONDS` | 30 | Fresh cache duration in seconds |
| `CACHE_STALE_TTL_SECONDS` | 120 | Stale-while-revalidate window in seconds |
| `CACHE_MAX_ENTRIES` | 500 | Maximum cached request variants |
| `RECIPES_DIR` | `./recipes` (or bundled defaults in installed package) | Path to recipes directory |
| `PLUGIN_ENFORCE_COMPATIBILITY` | false | Skip plugin recipes outside declared `web2api` version bounds |
| `BIRD_AUTH_TOKEN` | empty | X/Twitter auth token for `x` recipe |
| `BIRD_CT0` | empty | X/Twitter ct0 token for `x` recipe |

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
