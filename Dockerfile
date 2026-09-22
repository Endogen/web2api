FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.6.11 \
    && uv sync --frozen --no-dev --no-install-project

COPY web2api/ ./web2api/
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim AS runtime

ARG WEB2API_UID=1000
ARG WEB2API_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:${PATH}"
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/web2api /app/web2api

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && /app/.venv/bin/playwright install-deps chromium \
    && /app/.venv/bin/playwright install chromium --only-shell \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${WEB2API_GID}" web2api \
    && useradd --uid "${WEB2API_UID}" --gid "${WEB2API_GID}" --create-home web2api \
    && mkdir -p /data/recipes \
    && chown -R web2api:web2api /app /data /ms-playwright

USER web2api

EXPOSE 8000

CMD ["uvicorn", "web2api.main:app", "--host", "0.0.0.0", "--port", "8000"]
