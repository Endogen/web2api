# Unified REST API Schema

## Endpoints

### Read

```
GET /{site}/read?page=1
```

Fetches content/items from the target site. Pagination is 1-indexed from the caller's perspective (the recipe handles mapping internally).

### Search

```
GET /{site}/search?q=python&page=1
```

Searches the target site. Requires the recipe to declare `search` in its capabilities. Returns 400 if the recipe doesn't support search.

### Index / Discovery

```
GET /
```

HTML page listing all available recipes with their metadata, supported capabilities, and links to try them.

```
GET /api/sites
```

JSON listing of all available recipes and their metadata (for programmatic discovery).

## Unified JSON Response

Every endpoint returns the same top-level schema:

```json
{
  "site": {
    "name": "Hacker News",
    "slug": "hackernews",
    "url": "https://news.ycombinator.com"
  },
  "endpoint": "read",
  "query": null,
  "items": [
    {
      "title": "Show HN: I built a thing",
      "url": "https://example.com/thing",
      "fields": {
        "score": 153,
        "author": "pg",
        "comment_count": 42,
        "time_ago": "3 hours ago",
        "id": "39281234"
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

### Field Descriptions

#### `site`
| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable site name |
| `slug` | string | URL slug / recipe folder name |
| `url` | string | Base URL of the target site |

#### `endpoint`
String. Either `"read"` or `"search"`.

#### `query`
String or null. The search query (null for read endpoints).

#### `items`
Array of item objects. Each item has:

| Field | Type | Description |
|-------|------|-------------|
| `title` | string \| null | Primary title/name of the item |
| `url` | string \| null | Link to the item (absolute URL) |
| `fields` | object | All other extracted fields as key-value pairs |

The `title` and `url` fields are promoted to top-level in each item for consistency. All other recipe-defined fields go into `fields`. Values in `fields` can be strings, numbers, booleans, or null.

#### `pagination`
| Field | Type | Description |
|-------|------|-------------|
| `current_page` | int | Current page number (1-indexed) |
| `has_next` | bool | Whether there's a next page |
| `has_prev` | bool | Whether there's a previous page |
| `total_pages` | int \| null | Total pages if known |
| `total_items` | int \| null | Total item count if known |

#### `metadata`
| Field | Type | Description |
|-------|------|-------------|
| `scraped_at` | string | ISO 8601 timestamp of when scraping occurred |
| `response_time_ms` | int | Total time in milliseconds for the scrape |
| `item_count` | int | Number of items returned |
| `cached` | bool | Whether this response came from cache (future-proofing, always false for v1) |

#### `error`
Null on success. On failure:

```json
{
  "error": {
    "code": "SCRAPE_FAILED",
    "message": "Target site returned HTTP 503",
    "details": "Service Unavailable — the target site may be down"
  }
}
```

Error codes:
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `SITE_NOT_FOUND` | 404 | No recipe exists for the given slug |
| `CAPABILITY_NOT_SUPPORTED` | 400 | Recipe doesn't support the requested capability (e.g. search) |
| `SCRAPE_FAILED` | 502 | Playwright failed to scrape the target site |
| `SCRAPE_TIMEOUT` | 504 | Scraping exceeded timeout |
| `INVALID_PARAMS` | 400 | Invalid query parameters |
| `INTERNAL_ERROR` | 500 | Unexpected server error |

## HTTP Status Codes

| Status | When |
|--------|------|
| 200 | Successful scrape (even if 0 items returned) |
| 400 | Invalid parameters or unsupported capability |
| 404 | Unknown site slug |
| 502 | Target site error during scraping |
| 504 | Scraping timeout |
| 500 | Internal server error |
