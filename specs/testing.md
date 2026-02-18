# Testing Specification

## Strategy

Three levels of testing, all using pytest:

1. **Unit tests** — Pure logic: config parsing, transforms, URL building, response assembly
2. **Integration tests** — Server + mock HTML: recipe loading, scraping engine against local HTML fixtures, API endpoints
3. **E2E tests** — Docker container + real web: spin up the Docker container, hit real HN endpoints, validate responses

## Unit Tests

### Config Parsing (`tests/unit/test_config.py`)
- Valid YAML parses into correct Pydantic models
- Invalid YAML raises validation errors
- Missing required fields are caught
- Optional fields default correctly
- Slug must match folder name

### Transforms (`tests/unit/test_transforms.py`)
- `regex_int`: "153 points" → 153, "no match" → None
- `regex_float`: "$19.99" → 19.99
- `strip`: whitespace handling
- `strip_html`: tag removal
- `absolute_url`: relative → absolute conversion
- `iso_date`: various date formats
- Unknown transform raises error

### URL Building (`tests/unit/test_url_builder.py`)
- Page substitution (1-indexed and 0-indexed)
- Query substitution with URL encoding
- Base URL handling

### Response Assembly (`tests/unit/test_response.py`)
- Items correctly mapped to unified schema
- Title and URL promoted, rest in fields
- Pagination object built correctly
- Error response format
- Metadata populated

## Integration Tests

### Recipe Discovery (`tests/integration/test_discovery.py`)
- Discovers valid recipes from a test fixtures directory
- Skips invalid recipes with warnings
- Empty directory = no recipes
- Registry lookup by slug works
- Duplicate slugs are handled (warn + skip)

### Scraping Engine (`tests/integration/test_scraping.py`)
Use a local HTTP server (pytest-httpserver or similar) serving static HTML fixtures:
- Extract items from simple HTML matching a recipe config
- Handle `next_sibling` context
- Handle optional fields (element missing → null)
- Apply transforms correctly
- Execute actions (wait for selector in fixture HTML)
- Pagination detection

### API Endpoints (`tests/integration/test_api.py`)
Use FastAPI's `TestClient` (httpx) with mock recipes:
- `GET /{slug}/read` returns valid unified schema
- `GET /{slug}/search?q=test` returns valid unified schema
- `GET /unknown/read` returns 404 with `SITE_NOT_FOUND`
- `GET /{slug}/search` on read-only recipe returns 400 with `CAPABILITY_NOT_SUPPORTED`
- `GET /api/sites` lists all recipes
- `GET /` returns HTML index page
- Missing `q` param on search returns 400

### Browser Pool (`tests/integration/test_pool.py`)
- Pool starts and creates contexts
- Acquire/release cycle works
- Concurrent acquire respects max_contexts
- Timeout when pool exhausted
- Context recycling after TTL
- Pool health reports correct stats

## E2E Tests

### Docker-based (`tests/e2e/test_e2e.py`)
These tests:
1. Build and start the Docker container with `docker compose up`
2. Wait for the health endpoint to respond
3. Call `GET /hackernews/read` and validate:
   - Response matches unified schema (Pydantic validation)
   - `items` is non-empty
   - Each item has `title` (non-empty string)
   - Each item has `url` (valid URL)
   - `pagination.current_page` == 1
   - `pagination.has_next` == True
   - `metadata.item_count` > 0
4. Call `GET /hackernews/read?page=2` and validate pagination
5. Call `GET /hackernews/search?q=python` and validate:
   - Response matches unified schema
   - `endpoint` == "search"
   - `query` == "python"
   - Items are returned
6. Call `GET /api/sites` and validate hackernews is listed
7. Tear down the container

### Fixtures Directory Structure

```
tests/
├── unit/
│   ├── test_config.py
│   ├── test_transforms.py
│   ├── test_url_builder.py
│   └── test_response.py
├── integration/
│   ├── test_discovery.py
│   ├── test_scraping.py
│   ├── test_api.py
│   ├── test_pool.py
│   └── fixtures/
│       ├── valid_recipe/
│       │   └── recipe.yaml
│       ├── invalid_recipe/
│       │   └── recipe.yaml
│       ├── custom_scraper_recipe/
│       │   ├── recipe.yaml
│       │   └── scraper.py
│       └── html/
│           ├── simple_list.html
│           ├── sibling_data.html
│           └── paginated.html
└── e2e/
    └── test_e2e.py
```

## Coverage Target

≥ 80% line coverage across the codebase. Measured with `pytest-cov`:

```bash
pytest --cov=web2api --cov-report=term-missing tests/unit tests/integration
```

E2E tests are run separately (require Docker):

```bash
pytest tests/e2e -v
```
