# Hacker News Recipe (Reference Template)

This recipe demonstrates a complete Web2API integration with both `read` and `search` capabilities. Use it as a starting point when creating recipes for new sites.

## Files

- `recipe.yaml`: Declarative scraping configuration used by Web2API.
- `README.md`: Notes for how and why this recipe is structured.

## What It Scrapes

- `GET /hackernews/read?page=1`
  - Source: `https://news.ycombinator.com/news?p={page}`
  - Scrapes front-page story rows from Hacker News.
- `GET /hackernews/search?q=python&page=1`
  - Source: `https://hn.algolia.com/?q={query}&page={page_zero}`
  - Scrapes Algolia-backed HN search results.

## Why This Recipe Is a Good Template

1. It shows both endpoint types (`read` and `search`) in one recipe.
2. It demonstrates multi-row extraction via `context: next_sibling`.
3. It uses common transforms (`absolute_url`, `regex_int`) on real-world data.
4. It shows page-index mapping differences:
   - `read` uses `start: 1` with `{page}`
   - `search` uses `start: 0` with `{page_zero}`

## Key Extraction Patterns

### 1) Container + sibling metadata

HN front-page story content spans two table rows. The recipe uses:

- `container: "tr.athing"` for main story rows
- `context: "next_sibling"` for metadata fields in the following row (`score`, `author`, `comment_count`, `time_ago`)

This pattern is useful for sites where item data is split across nearby DOM nodes.

### 2) Optional fields

Several fields are marked `optional: true` to avoid hard failures when data is missing.

### 3) Numeric normalization

`regex_int` converts values like `"153 points"` and `"42 comments"` into integers.

### 4) URL normalization

`absolute_url` ensures relative links are converted to absolute URLs.

## Minimal Recipe Skeleton (Copy/Adapt)

```yaml
name: "Your Site"
slug: "yoursite"
base_url: "https://example.com"
description: "What this recipe provides"
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
    actions:
      - type: wait
        selector: ".result"
        timeout: 10000
    items:
      container: ".result"
      fields:
        title:
          selector: "a"
          attribute: "text"
        url:
          selector: "a"
          attribute: "href"
          transform: "absolute_url"
    pagination:
      type: "page_param"
      param: "page"
      start: 0
```

## Creating a New Recipe from This Template

1. Copy `recipes/hackernews` to `recipes/<new-slug>`.
2. Update `name`, `slug`, `base_url`, and `description`.
3. Replace URLs, selectors, and transforms for the target site.
4. Keep `slug` in `recipe.yaml` equal to the folder name.
5. Restart the server so discovery re-scans the `recipes/` directory.

## Verification

Run:

```bash
source .venv/bin/activate && pytest tests/integration/test_hackernews_live.py -v
```

The live test is gated behind `WEB2API_RUN_LIVE_HN_TESTS=1` and skips automatically when DNS/browser sandbox limits prevent real-site access.
