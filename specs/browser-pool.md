# Browser Pool Specification

## Problem

Playwright browser instances are expensive. Spawning one per request doesn't scale — each Chromium instance uses ~100–200 MB RAM. We need a pool that reuses browser contexts efficiently.

## Design

### Architecture

```
BrowserPool
├── 1 Chromium browser instance (shared, long-lived)
├── N browser contexts (isolated, reusable)
│   ├── Context 1 → Page (assigned to request A)
│   ├── Context 2 → Page (assigned to request B)
│   └── ...
└── Configuration
    ├── max_contexts: 5 (default)
    ├── context_timeout: 30s
    └── page_timeout: 15s
```

### Key Concepts

- **One browser, many contexts**: Launch a single Chromium instance. Each scraping request gets an isolated `BrowserContext` (separate cookies, storage, etc.)
- **Context pool**: Pre-create a configurable number of contexts. Requests check out a context, use it, then return it.
- **Context recycling**: After each use, close the page (not the context) and clear cookies/storage. If a context has been used N times, close and recreate it to prevent memory leaks.
- **Backpressure**: If all contexts are busy, new requests wait in a queue with a configurable timeout. If the queue is full or timeout expires, return 503 immediately.

### Interface

```python
class BrowserPool:
    async def start(self) -> None:
        """Launch browser and pre-create contexts."""

    async def stop(self) -> None:
        """Close all contexts and the browser."""

    async def acquire(self, timeout: float = 30.0) -> Page:
        """Get a fresh Page from the pool. Blocks until available or timeout."""

    async def release(self, page: Page) -> None:
        """Return a Page to the pool after use."""

    @asynccontextmanager
    async def page(self, timeout: float = 30.0) -> AsyncGenerator[Page, None]:
        """Context manager: acquire + auto-release."""
```

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `POOL_MAX_CONTEXTS` | `5` | Maximum concurrent browser contexts |
| `POOL_CONTEXT_TTL` | `50` | Recycle context after this many uses |
| `POOL_ACQUIRE_TIMEOUT` | `30` | Seconds to wait for a free context |
| `POOL_PAGE_TIMEOUT` | `15000` | Default Playwright page timeout (ms) |
| `POOL_QUEUE_SIZE` | `20` | Max pending requests in queue |

### Lifecycle

1. **Server startup**: `BrowserPool.start()` — launches Chromium, creates initial contexts
2. **Request arrives**: `pool.acquire()` → get a context → create a Page → scrape → close Page → `pool.release()`
3. **Context recycling**: After `POOL_CONTEXT_TTL` uses, close and recreate the context
4. **Server shutdown**: `BrowserPool.stop()` — close everything gracefully

### Error Handling

- If the browser process crashes, detect it and relaunch automatically
- If a page navigation fails, catch the error, release the context back to the pool, and return a `SCRAPE_FAILED` error
- If a context becomes corrupted (e.g., stuck page), force-close and recreate it

### Health Check

The pool should expose a health status:

```python
@property
def health(self) -> dict:
    return {
        "browser_connected": True,
        "total_contexts": 5,
        "available_contexts": 3,
        "queue_size": 0,
        "total_requests_served": 142
    }
```

This feeds into `GET /health` and the index page.
