# Scraping Engine Specification

## Overview

The scraping engine is the core that executes recipes — it takes a recipe config, a browser page, and request parameters, then returns structured data matching the unified response schema.

## Flow

```
Request → Route Handler → Scraping Engine → Browser Pool (get page) → Execute Actions → Extract Items → Transform Fields → Build Response → Release Page → Return
```

### Detailed Steps

1. **Resolve recipe**: Look up the recipe from the registry
2. **Acquire page**: Get a Playwright `Page` from the browser pool
3. **Build URL**: Substitute `{page}`, `{query}`, `{page_zero}` placeholders in the endpoint URL
4. **Navigate**: `page.goto(url)` with timeout
5. **Execute actions**: Run the action sequence (wait, click, scroll, etc.)
6. **Extract items**: Query the DOM using the container selector, then extract fields from each item
7. **Transform fields**: Apply transforms (regex_int, strip, absolute_url, etc.)
8. **Detect pagination**: Determine if there's a next page
9. **Build response**: Assemble the unified JSON response
10. **Release page**: Return the page to the browser pool

## URL Template Variables

| Variable | Source | Description |
|----------|--------|-------------|
| `{page}` | `page` query param (default: 1) | Page number (1-indexed from API, mapped per recipe) |
| `{page_zero}` | `page` query param - 1 | Zero-indexed page number |
| `{query}` | `q` query param | URL-encoded search query |

## Action Executor

Processes the `actions` list from the recipe config sequentially:

```python
async def execute_actions(page: Page, actions: list[Action]) -> None:
    for action in actions:
        match action.type:
            case "wait":
                await page.wait_for_selector(action.selector, timeout=action.timeout)
            case "click":
                await page.click(action.selector)
            case "scroll":
                await page.evaluate(f"window.scrollBy(0, {action.amount})")
            case "type":
                await page.fill(action.selector, action.text)
            case "sleep":
                await page.wait_for_timeout(action.ms)
            case "evaluate":
                await page.evaluate(action.script)
```

## Item Extractor

Extracts structured data from the page:

```python
async def extract_items(page: Page, items_config: ItemsConfig) -> list[dict]:
    containers = await page.query_selector_all(items_config.container)
    results = []
    for container in containers:
        item = {}
        for field_name, field_config in items_config.fields.items():
            element = resolve_element(container, field_config)
            raw_value = await get_attribute(element, field_config.attribute)
            item[field_name] = apply_transform(raw_value, field_config.transform)
        results.append(item)
    return results
```

### Element Resolution

For fields with `context` other than `self`, resolve the target element:

- `self`: `container.query_selector(selector)`
- `next_sibling`: Get the next sibling of the container, then query within it
- `parent`: Get the parent of the container, then query within it

### Attribute Extraction

| Attribute | Method |
|-----------|--------|
| `text` | `element.text_content()` |
| `href` | `element.get_attribute("href")` |
| `src` | `element.get_attribute("src")` |
| any other | `element.get_attribute(attr_name)` |

## Custom Scraper Integration

When a recipe has a `scraper.py`:

1. Check if the `Scraper` class implements the requested method (`read` or `search`)
2. If yes: call `scraper.read(page, params)` or `scraper.search(page, params)` — the custom scraper has full control
3. If no: fall back to the declarative config for that endpoint
4. The custom scraper receives a page from the pool (already launched, not yet navigated)

```python
class BaseScraper:
    """Base class for custom scrapers."""

    async def read(self, page: Page, params: dict) -> ScrapeResult:
        raise NotImplementedError

    async def search(self, page: Page, params: dict) -> ScrapeResult:
        raise NotImplementedError

    def supports_read(self) -> bool:
        return type(self).read is not BaseScraper.read

    def supports_search(self) -> bool:
        return type(self).search is not BaseScraper.search
```

## Pagination Detection

After extracting items, determine pagination state:

### `page_param` / `offset_param`
- `has_next`: True if items were found (non-empty result)
- `has_prev`: True if current page > start page
- `total_pages`: null (unknown)

### `next_link`
- `has_next`: True if the `selector` matches an element on the page
- The next page URL is extracted from the link's `href`

## Timeouts

| Operation | Default | Configurable |
|-----------|---------|-------------|
| Page navigation | 15s | `POOL_PAGE_TIMEOUT` |
| Action execution | Per-action timeout | In action config |
| Total scrape | 30s | `SCRAPE_TIMEOUT` |

If any timeout is exceeded, release the page and return a `SCRAPE_TIMEOUT` error.

## Error Handling

- **Navigation failure** (DNS, HTTP error): Return `SCRAPE_FAILED` with details
- **Selector not found** (for required fields): Return `SCRAPE_FAILED`
- **Selector not found** (for optional fields): Set field value to `null`
- **Transform failure**: Set field value to `null`, log warning
- **Custom scraper exception**: Catch, log, return `SCRAPE_FAILED`
