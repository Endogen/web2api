# Docker Specification

## Dockerfile

Single-stage Dockerfile (Playwright needs the browser binaries):

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install Playwright system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # Playwright Chromium deps will be handled by playwright install-deps
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Install Playwright browsers
RUN playwright install chromium && playwright install-deps chromium

# Copy application code
COPY web2api/ ./web2api/
COPY recipes/ ./recipes/

EXPOSE 8000

CMD ["uvicorn", "web2api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## docker-compose.yml

```yaml
services:
  web2api:
    build: .
    ports:
      - "8000:8000"
    environment:
      POOL_MAX_CONTEXTS: "${POOL_MAX_CONTEXTS:-5}"
      POOL_PAGE_TIMEOUT: "${POOL_PAGE_TIMEOUT:-15000}"
      SCRAPE_TIMEOUT: "${SCRAPE_TIMEOUT:-30}"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 15s
```

## Environment Variables

All runtime-configurable via environment:

| Variable | Default | Description |
|----------|---------|-------------|
| `POOL_MAX_CONTEXTS` | `5` | Max concurrent browser contexts |
| `POOL_CONTEXT_TTL` | `50` | Recycle context after N uses |
| `POOL_ACQUIRE_TIMEOUT` | `30` | Seconds to wait for a free context |
| `POOL_PAGE_TIMEOUT` | `15000` | Playwright page timeout (ms) |
| `POOL_QUEUE_SIZE` | `20` | Max queued requests |
| `SCRAPE_TIMEOUT` | `30` | Total scrape timeout (seconds) |
| `RECIPES_DIR` | `./recipes` | Path to recipes directory |
| `LOG_LEVEL` | `info` | Logging level |

## Health Endpoint

`GET /health` returns:

```json
{
  "status": "healthy",
  "browser": {
    "connected": true,
    "total_contexts": 5,
    "available_contexts": 4,
    "queue_size": 0
  },
  "recipes": {
    "count": 3,
    "slugs": ["hackernews", "wikipedia", "reddit"]
  },
  "uptime_seconds": 3600
}
```
