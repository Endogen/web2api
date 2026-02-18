# Recipe Format Specification

## Folder Structure

Each recipe lives in a folder under `recipes/`:

```
recipes/
├── hackernews/
│   ├── recipe.yaml       # Declarative config (required)
│   ├── scraper.py        # Custom scraper override (optional)
│   └── README.md         # Human-readable docs (optional)
├── wikipedia/
│   ├── recipe.yaml
│   └── scraper.py
└── ...
```

The recipe folder name becomes the API route slug: `recipes/hackernews/` → `GET /hackernews/read`.

## recipe.yaml Schema

```yaml
# Site metadata
name: "Hacker News"
slug: "hackernews"           # URL slug (must match folder name)
base_url: "https://news.ycombinator.com"
description: "Hacker News front page stories and search"

# Which capabilities this recipe supports
capabilities:
  - read
  - search

# Endpoint definitions
endpoints:
  read:
    url: "https://news.ycombinator.com/news?p={page}"
    # Playwright actions to execute before scraping (in order)
    actions:
      - type: wait
        selector: ".itemlist"
        timeout: 5000
    # Item extraction
    items:
      container: ".athing"          # CSS selector for each item container
      fields:
        title:
          selector: ".titleline > a"
          attribute: "text"          # "text", "href", "src", or any HTML attribute
        url:
          selector: ".titleline > a"
          attribute: "href"
        score:
          selector: ".score"
          attribute: "text"
          # Relative selector: resolved from sibling/parent context
          context: "next_sibling"    # Look in the next <tr> sibling
          transform: "regex_int"     # Extract integer from text (e.g., "153 points" → 153)
          optional: true
        author:
          selector: ".hnuser"
          context: "next_sibling"
          attribute: "text"
          optional: true
        comment_count:
          selector: "a[href^='item?id=']:last-child"
          context: "next_sibling"
          attribute: "text"
          transform: "regex_int"
          optional: true
        time_ago:
          selector: ".age"
          context: "next_sibling"
          attribute: "text"
          optional: true
        id:
          selector: ""               # The container element itself
          attribute: "id"

    # Pagination
    pagination:
      type: "page_param"            # "page_param", "next_link", or "offset_param"
      param: "page"                  # URL parameter name
      start: 1                       # First page number

  search:
    # Hacker News doesn't have built-in search on the main site,
    # but the Algolia-powered search at hn.algolia.com is part of HN.
    # For the reference implementation, we scrape the search page.
    url: "https://hn.algolia.com/?q={query}&page={page_zero}"
    actions:
      - type: wait
        selector: ".Story"
        timeout: 10000
    items:
      container: ".Story"
      fields:
        title:
          selector: ".Story_title a:first-child"
          attribute: "text"
        url:
          selector: ".Story_title a:first-child"
          attribute: "href"
        score:
          selector: ".Story_meta span:first-child"
          attribute: "text"
          transform: "regex_int"
          optional: true
        author:
          selector: ".Story_meta a[href^='https://news.ycombinator.com/user']"
          attribute: "text"
          optional: true
        time_ago:
          selector: ".Story_meta span[title]"
          attribute: "text"
          optional: true

    pagination:
      type: "page_param"
      param: "page"
      start: 0                       # Zero-indexed pages
```

## Action Types

Actions are Playwright operations executed before data extraction:

| Type | Parameters | Description |
|------|-----------|-------------|
| `wait` | `selector`, `timeout` | Wait for element to appear |
| `click` | `selector` | Click an element |
| `scroll` | `direction` (down/up), `amount` (pixels or "bottom") | Scroll the page |
| `type` | `selector`, `text` | Type text into an input |
| `sleep` | `ms` | Wait fixed milliseconds |
| `evaluate` | `script` | Run arbitrary JavaScript |

## Field Transforms

| Transform | Description | Example |
|-----------|-------------|---------|
| `regex_int` | Extract first integer from text | "153 points" → `153` |
| `regex_float` | Extract first float from text | "$19.99" → `19.99` |
| `strip` | Strip whitespace (default) | "  hello  " → `"hello"` |
| `strip_html` | Remove HTML tags | `<b>hello</b>` → `"hello"` |
| `iso_date` | Parse to ISO 8601 date string | "Jan 5, 2026" → `"2026-01-05"` |
| `absolute_url` | Convert relative URL to absolute | `/page` → `https://site.com/page` |

## Field Context

For sites where data isn't nested inside the item container (like HN where each story spans two `<tr>` rows):

| Context | Description |
|---------|-------------|
| `self` (default) | Look inside the container element |
| `next_sibling` | Look in the next sibling element |
| `parent` | Look in the parent element |

## Pagination Types

| Type | Parameters | Description |
|------|-----------|-------------|
| `page_param` | `param`, `start` | Append `?param=N` to URL, increment N |
| `next_link` | `selector` | Extract href from a "next page" link |
| `offset_param` | `param`, `start`, `step` | Append `?param=N`, increment by step |

## Optional scraper.py

When the declarative config isn't enough, a recipe can include `scraper.py` with a class that implements the scraping interface:

```python
from web2api.scraping import BaseScraper, ScrapeResult
from playwright.async_api import Page

class Scraper(BaseScraper):
    """Custom scraper for sites needing complex interaction."""

    async def read(self, page: Page, params: dict) -> ScrapeResult:
        """Scrape items for the read endpoint."""
        # Full Playwright control here
        ...

    async def search(self, page: Page, params: dict) -> ScrapeResult:
        """Scrape search results."""
        ...
```

Rules:
- If `scraper.py` exists and defines a `Scraper` class, it **overrides** the declarative config for the endpoints it implements
- It can implement just `read`, just `search`, or both
- If it only implements one, the other falls back to the declarative config
- It receives a Playwright `Page` from the browser pool (already navigated if `url` is set in YAML)
- Must return a `ScrapeResult` matching the unified response schema
