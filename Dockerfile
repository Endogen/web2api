FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @steipete/bird \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY web2api/ ./web2api/

RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium

COPY recipes/ ./recipes/

EXPOSE 8000

CMD ["uvicorn", "web2api.main:app", "--host", "0.0.0.0", "--port", "8000"]
