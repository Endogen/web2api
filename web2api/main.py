"""FastAPI application entrypoint."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Web2API",
        summary="Turn websites into REST APIs by scraping them live with Playwright.",
        version="0.1.0",
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Basic health endpoint used for startup verification."""
        return {"status": "ok"}

    return app


app = create_app()
