"""Live integration test for the Hacker News reference recipe."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from web2api.engine import scrape
from web2api.pool import BrowserPool
from web2api.registry import RecipeRegistry
from web2api.schemas import ApiResponse

LIVE_HN_ENV = "WEB2API_RUN_LIVE_HN_TESTS"
_ENVIRONMENT_ERROR_MARKERS = (
    "err_name_not_resolved",
    "could not resolve",
    "name or service not known",
    "dns",
    "connection refused",
    "connection reset",
    "sandbox_host_linux.cc",
)


def _live_hn_enabled() -> bool:
    return os.getenv(LIVE_HN_ENV) == "1"


def _assert_live_result_or_skip(response: ApiResponse, *, endpoint: str) -> None:
    if response.error is None:
        return

    error_message = response.error.message.lower()
    if any(marker in error_message for marker in _ENVIRONMENT_ERROR_MARKERS):
        pytest.skip(f"Live environment unavailable for {endpoint}: {response.error.message}")

    pytest.fail(
        f"Live {endpoint} scrape failed with {response.error.code}: {response.error.message}",
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _live_hn_enabled(),
    reason=f"Set {LIVE_HN_ENV}=1 to run live Hacker News integration checks.",
)
async def test_hackernews_recipe_live_scrape() -> None:
    registry = RecipeRegistry()
    registry.discover(Path("recipes"))
    recipe = registry.get("hackernews")
    assert recipe is not None

    pool = BrowserPool(max_contexts=1, acquire_timeout=20.0, page_timeout_ms=20_000)
    try:
        try:
            await pool.start()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Live environment unavailable: unable to start browser ({exc})")

        read_response = await scrape(pool=pool, recipe=recipe, endpoint="read", page=1)
        _assert_live_result_or_skip(read_response, endpoint="read")
        assert read_response.error is None
        assert read_response.metadata.item_count > 0
        assert read_response.items
        assert read_response.items[0].title
        assert read_response.items[0].url
        assert read_response.pagination.current_page == 1

        search_response = await scrape(
            pool=pool,
            recipe=recipe,
            endpoint="search",
            query="python",
            page=1,
        )
        _assert_live_result_or_skip(search_response, endpoint="search")
        assert search_response.error is None
        assert search_response.query == "python"
        assert search_response.items
        assert search_response.items[0].title
    finally:
        await pool.stop()
