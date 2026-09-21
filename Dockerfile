FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.6.11 \
    && uv sync --frozen --no-dev --no-install-project

COPY web2api/ ./web2api/

ENV PATH="/app/.venv/bin:${PATH}"

RUN uv sync --frozen --no-dev --no-editable \
    && playwright install --with-deps chromium

RUN mkdir -p /data/recipes

EXPOSE 8000

CMD ["uvicorn", "web2api.main:app", "--host", "0.0.0.0", "--port", "8000"]
