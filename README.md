# Web2API

Turn any website into a REST API by scraping it live with Playwright.

Web2API starts with no recipes installed by default. Recipes are installed into a local recipes
directory from a catalog source (local path or git repo), then discovered at runtime. Each recipe
defines endpoints with selectors, actions, fields, and pagination in YAML. Optional Python
scrapers handle interactive or complex sites. Optional plugin metadata can declare external
dependencies and required env vars.

![Recipe Repository](docs/screenshots/repository.png)
*Recipe Repository — browse and install available recipes from the catalog.*

![Installed APIs](docs/screenshots/installed.png)
*Installed APIs — active recipes with their API endpoints and copy-to-clipboard URLs.*

## Terminology

- **Recipe**: a site integration folder (`recipe.yaml` + optional `scraper.py`) that exposes API
  endpoints.
- **Plugin metadata**: optional `plugin.yaml` inside a recipe that declares dependencies,
  healthchecks, and compatibility.

In this project, recipe lifecycle operations are always `recipes` commands. `plugin.yaml` is only
for optional dependency/runtime metadata inside a recipe.

## Features

- **Arbitrary named endpoints** — recipes define as many endpoints as needed (not limited to read/search)
- **Declarative YAML recipes** with selectors, actions, transforms, and pagination
- **Custom Python scrapers** for interactive sites (e.g. typing text, waiting for dynamic content)
- **Optional plugin metadata** (`plugin.yaml`) for recipe-specific dependency requirements
- **Shared browser/context pool** for concurrent Playwright requests
- **In-memory response cache** with stale-while-revalidate
- **Unified JSON response schema** across all recipes and endpoints
- **Docker deployment** with auto-restart

## Quickstart (Local)

```bash
git clone https://github.com/Endogen/web2api.git
cd web2api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install --with-deps chromium
```

Start the service:

```bash
uvicorn web2api.main:app --host 0.0.0.0 --port 8010
```

Install recipes (in a separate terminal):

```bash
web2api recipes catalog list
web2api recipes catalog add hackernews --yes
```

Service: `http://localhost:8010`

### Verify

```bash
curl -s http://localhost:8010/health | jq
curl -s http://localhost:8010/api/sites | jq
```

## Quickstart (Docker)

```bash
git clone https://github.com/Endogen/web2api.git
cd web2api
export WEB2API_ADMIN_TOKEN="replace-with-a-long-random-token"
docker compose up --build -d
```

Service: `http://localhost:8010`

### Verify

```bash
curl -s http://localhost:8010/health | jq
curl -s http://localhost:8010/api/sites | jq
```

Install recipes via the CLI inside the container:

```bash
docker compose exec web2api web2api recipes catalog list
docker compose exec web2api web2api recipes catalog add hackernews --yes
```

> **Note:** When using Docker, all `web2api` CLI commands must be prefixed with
> `docker compose exec web2api` since the CLI is installed inside the container.

## Server Setup (Recommended Pattern)

1. Provision host with Python 3.12+, Chromium dependencies, and optional Docker/Nginx.
2. Set persistent recipe storage (`RECIPES_DIR`, for example `/var/lib/web2api/recipes`).
3. Use the default official catalog repo (`https://github.com/Endogen/web2api-recipes.git`) or
   override via `WEB2API_RECIPE_CATALOG_SOURCE` (plus optional
   `WEB2API_RECIPE_CATALOG_REF` / `WEB2API_RECIPE_CATALOG_PATH`).
4. Run Web2API as a long-lived process (systemd, container, or supervisor).
5. Install initial recipes via CLI/API/UI.
6. Put reverse proxy/TLS in front (Nginx/Caddy/Traefik) for production.

## Access Token

Web2API can protect all HTTP routes except selected public paths with a shared access token.

The recipe-management API is fail-closed even when general route authentication is disabled.
Set `WEB2API_ADMIN_TOKEN` (or the general `WEB2API_ACCESS_TOKEN`) before using
`/api/recipes/manage`. Development-only deployments can opt out with
`WEB2API_ALLOW_UNAUTHENTICATED_ADMIN=true`.

Set one of:
- `WEB2API_ACCESS_TOKEN`
- `WEB2API_ACCESS_TOKEN_FILE` (path to a file containing the token)

By default, when configured, Web2API requires the token for everything except:
- `/`
- `/health`

You can keep extra routes public while token auth stays enabled by setting
`WEB2API_PUBLIC_PATHS` to a comma- or newline-separated list of exact paths or shell-style
glob patterns matched against the request path.

Common examples:
- `/api/sites`
- `/docs`
- `/openapi.json`
- `/allenai/*`
- `/*/chat`

Any path that matches one of those patterns skips token auth, so use this allowlist sparingly.

Send the token as either:
- `Authorization: Bearer <token>`
- `X-Web2API-Key: <token>`

Example mixed setup:

```bash
export WEB2API_ACCESS_TOKEN="secret-token"
export WEB2API_PUBLIC_PATHS="/api/sites,/allenai/*,/docs,/openapi.json"
```

Authenticated request examples:

```bash
curl -H "Authorization: Bearer $WEB2API_ACCESS_TOKEN" http://localhost:8010/allenai/chat?q=example&page=1
curl -H "Authorization: Bearer $WEB2API_ACCESS_TOKEN" http://localhost:8010/api/recipes/manage
curl -H "Authorization: Bearer $WEB2API_ACCESS_TOKEN" http://localhost:8010/mcp/tools
```

When token auth is enabled, the built-in web UI shows an access-token input and stores the token in
browser local storage for protected browser actions.

## CLI

Web2API ships with a management CLI:

```bash
web2api --help
```

### Recipe Commands (`recipes`)

```bash
# List all recipe folders with metadata readiness
web2api recipes list

# Check missing env vars/commands/packages
web2api recipes doctor
web2api recipes doctor x
web2api recipes doctor x --no-run-healthchecks
web2api recipes doctor x --allow-untrusted

# Install recipe from source
web2api recipes add ./my-recipe
web2api recipes add https://github.com/acme/web2api-recipes.git --ref v1.2.0 --subdir recipes/news

# Update managed recipe from recorded source
web2api recipes update x --yes
web2api recipes update x --ref v1.3.0 --subdir recipes/x --yes

# Install recipe from catalog
web2api recipes catalog list
web2api recipes catalog add hackernews --yes
web2api recipes catalog list --catalog-source https://github.com/acme/web2api-recipes.git

# Install declared dependencies from recipe metadata (host)
web2api recipes install x --yes
web2api recipes install x --apt --yes   # include apt packages

# Generate Dockerfile snippet for recipe metadata dependencies
web2api recipes install x --target docker --apt

# Remove recipe + manifest record
web2api recipes uninstall x --yes

# Disable/enable a recipe (writes/removes recipes/<slug>/.disabled)
web2api recipes disable x --yes
web2api recipes enable x
```

`recipes install` does not run `apt` installs unless `--apt` is explicitly passed.
Install-state records are stored in `<RECIPES_DIR>/.web2api_recipes.json`.
Default `RECIPES_DIR` is `~/.web2api/recipes`.
Catalog defaults come from:
- `WEB2API_RECIPE_CATALOG_SOURCE` (path or git URL)
- `WEB2API_RECIPE_CATALOG_REF` (optional git ref)
- `WEB2API_RECIPE_CATALOG_PATH` (catalog file path inside source, default `catalog.yaml`)
If `WEB2API_RECIPE_CATALOG_SOURCE` is unset, Web2API uses the official remote repo
`https://github.com/Endogen/web2api-recipes.git`.
For repeatable production installs, set `WEB2API_RECIPE_CATALOG_REF` to an immutable commit SHA;
the installer also records the installed recipe tree hash in its manifest.
`recipes update` works only for recipes tracked in the manifest.

Catalog entries can include optional setup hints:
- `requires_env`: list of required environment variable names (e.g. `["BIRD_AUTH_TOKEN", "BIRD_CT0"]`)
- `docs_url` (or `readme_url`): URL shown in CLI/UI as setup documentation

If `docs_url` is omitted and the recipe source resolves to GitHub, Web2API automatically
links to `<repo>/blob/<ref-or-HEAD>/<subdir>/README.md`.

Recipes installed from untrusted sources (for example git URLs) are blocked from executing
install/healthcheck commands unless `--allow-untrusted` is passed. Untrusted recipes also do not
load `scraper.py`; only declarative YAML endpoints are available until the recipe is trusted.

### Custom Local Recipes (Without Catalog)

You can use custom recipes without publishing them to the recipe repository:

```bash
# Direct local path install into RECIPES_DIR (tracked as source_type=local)
web2api recipes add ./my-recipe --yes

# Or copy folder manually into RECIPES_DIR/<slug> (unmanaged local recipe)
cp -r ./my-recipe "$RECIPES_DIR/<slug>"
```

Recipe origin visibility:
- `source_type=catalog|git|local` in manifest-backed installs
- `origin=unmanaged` for manual local folders not tracked in manifest
- The web UI manager shows both catalog recipes and local-only installed recipes

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

After `self update apply`, the CLI automatically runs `web2api recipes doctor`.


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

## MCP Server (Model Context Protocol)

Web2API includes a built-in MCP server that automatically exposes all installed recipes as
native tools for AI assistants. Every recipe endpoint becomes its own tool — no configuration
needed. Install a recipe, and it's instantly available as an MCP tool.

### Connecting Claude Desktop

Web2API uses [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http)
transport. Claude Desktop requires a local stdio bridge:

Add to your `claude_desktop_config.json`
([location](https://modelcontextprotocol.io/quickstart/user#configure-claude-for-desktop)):

Without access token:

```json
{
  "mcpServers": {
    "web2api": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-web2api-host/mcp/"]
    }
  }
}
```

With access token:

```json
{
  "mcpServers": {
    "web2api": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://your-web2api-host/mcp/",
        "--header", "Authorization: Bearer YOUR_TOKEN_HERE"
      ]
    }
  }
}
```

> **Requires Node.js ≥ 18** on the machine running Claude Desktop.

### Connecting Claude Code

Without access token:

```bash
claude mcp add --transport http web2api https://your-web2api-host/mcp/
```

With access token:

```bash
claude mcp add --transport http web2api https://your-web2api-host/mcp/ \
  --header "Authorization: Bearer YOUR_TOKEN_HERE"
```

### Connecting Other MCP Clients

Any MCP client that supports Streamable HTTP transport can connect directly:

```
URL: https://your-web2api-host/mcp/
Transport: Streamable HTTP
```

### How It Works

- Each recipe endpoint registers as a separate MCP tool  
  (e.g. `brave-search__search`, `deepl__de-en`, `allenai__olmo-32b`)
- Tools include proper descriptions and typed parameter schemas
- When recipes are installed/uninstalled via the admin API, tools rebuild automatically
- Optional access-token protection is available via `WEB2API_ACCESS_TOKEN`

### Example Tools

After installing the `brave-search` and `deepl` recipes:

| Tool | Description | Parameters |
|---|---|---|
| `brave-search__search` | Web search via Brave | `q` (required), `page` |
| `deepl__de-en` | Translate German → English | `q` (required), `page` |
| `deepl__en-de` | Translate English → German | `q` (required), `page` |

Legacy single-underscore tool calls are still resolved by the HTTP bridge. Native tool discovery
uses the unambiguous double-underscore form. For non-local MCP hostnames, configure
`WEB2API_MCP_ALLOWED_HOSTS` and `WEB2API_MCP_ALLOWED_ORIGINS` explicitly.

### HTTP Bridge (Legacy)

A simpler HTTP-based tool bridge is also available for non-MCP clients:

| Endpoint | Description |
|---|---|
| `GET /mcp/tools` | List all tools as JSON |
| `GET /mcp/tools?only=brave-search` | Filter by recipe slug |
| `GET /mcp/tools?exclude=allenai` | Exclude recipe slugs |
| `GET /mcp/exclude/{slugs}/tools` | Path-based exclusion filter |
| `GET /mcp/only/{slugs}/tools` | Path-based inclusion filter |
| `POST /mcp/tools/{tool_name}` | Call a tool (JSON body with params) |

## API

### Discovery

| Endpoint | Description |
|---|---|
| `GET /` | HTML index listing all recipes and endpoints (always public) |
| `GET /health` | Service, browser pool, and cache health (always public) |
| `GET /api/sites` | JSON list of all recipes with endpoint metadata (protected by default when token auth is enabled) |
| `GET /api/recipes/manage` | JSON catalog + installed recipe state for UI/automation (protected by default when token auth is enabled) |
| `POST /api/recipes/manage/install/{name}` | Install recipe by catalog entry name (protected by default when token auth is enabled) |
| `POST /api/recipes/manage/update/{slug}` | Update installed managed recipe (protected by default when token auth is enabled) |
| `POST /api/recipes/manage/uninstall/{slug}` | Uninstall recipe (add `?force=true` for unmanaged local recipes, protected by default when token auth is enabled) |
| `POST /api/recipes/manage/enable/{slug}` | Enable installed recipe (protected by default when token auth is enabled) |
| `POST /api/recipes/manage/disable/{slug}` | Disable installed recipe (protected by default when token auth is enabled) |

`GET /api/recipes/manage` includes:
- `catalog`: entries from the current catalog source
- `installed`: discovered recipes from `RECIPES_DIR`
- `origin`: one of `catalog`, `git`, `local`, `unmanaged`

### Recipe Endpoints

All recipe endpoints follow the pattern: `GET /{slug}/{endpoint}?page=1&q=...`
and require the access token by default when token auth is enabled.

- `page` — pagination (default: 1)
- `q` — query text (required when `requires_query: true`)
- additional query params are passed to custom scrapers
- extra query param names must be valid Python identifiers (and not keywords); values are capped at 512 chars

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
- Recipes installed via CLI/API/UI are loaded immediately
- If you edit recipe files manually on disk, restart the service to reload them
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
| `params` | no | Declared extra parameters; undeclared inputs are rejected |
| `accepts_files` | no | Allow bounded multipart uploads for this endpoint (default: `false`) |
| `actions` | no | Playwright actions to run before extraction |
| `items` | yes | Container selector + field definitions |
| `pagination` | yes | Pagination strategy (`page_param`, `offset_param`, or `next_link`) |

Pagination notes:
`{page}` resolves to `start + ((api_page - 1) * step)`.

Extra parameters support `type` (`string`, `integer`, `number`, `boolean`), `required`, `enum`,
`minimum`, `maximum`, `pattern`, `min_length`, `max_length`, and `example`. These constraints are
used consistently by REST validation and MCP schemas.

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

For a direct HTTP or CLI implementation, set `requires_browser = False`; its `page` argument is
then `None` and it runs under the bounded direct-executor semaphore instead of consuming a browser
context. Browser and direct outbound HTTP requests reject loopback, private, link-local, and
reserved destinations by default. Operators can explicitly allow internal providers with
`WEB2API_ALLOW_PRIVATE_NETWORK=true`.

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
    - "@steipete/bird@0.8.0"
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
| `DIRECT_MAX_CONCURRENCY` | 20 | Maximum concurrent direct HTTP/CLI scraper calls |
| `CACHE_ENABLED` | true | Enable in-memory response caching |
| `CACHE_TTL_SECONDS` | 30 | Fresh cache duration in seconds |
| `CACHE_STALE_TTL_SECONDS` | 120 | Stale-while-revalidate window in seconds |
| `CACHE_MAX_ENTRIES` | 500 | Maximum cached request variants |
| `RECIPES_DIR` | `~/.web2api/recipes` | Path to recipes directory |
| `WEB2API_RECIPE_CATALOG_SOURCE` | `https://github.com/Endogen/web2api-recipes.git` | Catalog source path or git URL |
| `WEB2API_RECIPE_CATALOG_REF` | empty | Optional git ref for catalog source |
| `WEB2API_RECIPE_CATALOG_PATH` | `catalog.yaml` | Catalog file path inside catalog source |
| `PLUGIN_ENFORCE_COMPATIBILITY` | false | Skip plugin recipes outside declared `web2api` version bounds |
| `WEB2API_ACCESS_TOKEN` | empty | Shared access token for all routes except public paths |
| `WEB2API_ACCESS_TOKEN_FILE` | empty | Path to file containing the access token (alternative to `WEB2API_ACCESS_TOKEN`) |
| `WEB2API_ADMIN_TOKEN` | empty | Separate token for recipe-management routes; admin is disabled if no general/admin token is set |
| `WEB2API_ALLOW_UNAUTHENTICATED_ADMIN` | false | Development-only opt-out for fail-closed admin routes |
| `WEB2API_PUBLIC_PATHS` | empty | Extra public path patterns to allow without auth while token auth is enabled |
| `WEB2API_ALLOW_PRIVATE_NETWORK` | false | Explicitly allow recipe traffic to private/internal network targets |
| `WEB2API_MAX_UPLOAD_FILES` | 4 | Maximum multipart files per request |
| `WEB2API_MAX_UPLOAD_BYTES` | 26214400 | Maximum bytes per uploaded file |
| `WEB2API_MCP_ALLOWED_HOSTS` | local hosts | Allowed MCP Host header patterns |
| `WEB2API_MCP_ALLOWED_ORIGINS` | local origins | Allowed MCP Origin patterns |
| `BIRD_AUTH_TOKEN` | empty | X/Twitter auth token for `x` recipe |
| `BIRD_CT0` | empty | X/Twitter ct0 token for `x` recipe |

## Testing

```bash
# Inside the container or with deps installed:
pytest tests/unit tests/integration --timeout=30 -x -q

# Explicit, bounded Docker/live-network suite
pytest -m e2e tests/e2e -v
```

## Tech Stack

- Python 3.12 + FastAPI + Playwright (Chromium)
- Pydantic for config validation
- Docker for deployment

## License

MIT
