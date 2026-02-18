"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from web2api.pool import BrowserPool


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    pool = BrowserPool()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await pool.start()
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.stop()

    app = FastAPI(
        title="Web2API",
        summary="Turn websites into REST APIs by scraping them live with Playwright.",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str | bool | int]:
        """Basic health endpoint used for startup verification."""
        return {"status": "ok", **pool.health}

    return app


app = create_app()
