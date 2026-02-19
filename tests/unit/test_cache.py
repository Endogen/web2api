"""Unit tests for response cache behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from web2api.cache import ResponseCache
from web2api.schemas import (
    ApiResponse,
    MetadataResponse,
    PaginationResponse,
    SiteInfo,
)


def _response(*, item_count: int = 0) -> ApiResponse:
    return ApiResponse(
        site=SiteInfo(name="Example", slug="example", url="https://example.com"),
        endpoint="read",
        query=None,
        items=[],
        pagination=PaginationResponse(
            current_page=1,
            has_next=False,
            has_prev=False,
            total_pages=None,
            total_items=None,
        ),
        metadata=MetadataResponse(
            scraped_at=datetime.now(UTC),
            response_time_ms=1,
            item_count=item_count,
            cached=False,
        ),
        error=None,
    )


@pytest.mark.asyncio
async def test_cache_returns_fresh_hit_after_set() -> None:
    cache = ResponseCache(ttl_seconds=1.0, stale_ttl_seconds=1.0, max_entries=8)
    key = ("example", "read", 1, None, ())

    miss = await cache.get(key)
    assert miss.state == "miss"

    await cache.set(key, _response(item_count=1))
    hit = await cache.get(key)
    assert hit.state == "fresh"
    assert hit.response is not None
    assert hit.response.metadata.item_count == 1


@pytest.mark.asyncio
async def test_cache_returns_stale_then_miss_after_windows_pass() -> None:
    cache = ResponseCache(ttl_seconds=0.03, stale_ttl_seconds=0.03, max_entries=8)
    key = ("example", "read", 1, None, ())
    await cache.set(key, _response(item_count=2))

    await asyncio.sleep(0.04)
    stale = await cache.get(key)
    assert stale.state == "stale"

    await asyncio.sleep(0.04)
    miss = await cache.get(key)
    assert miss.state == "miss"


@pytest.mark.asyncio
async def test_cache_triggers_background_refresh_for_stale_entry() -> None:
    cache = ResponseCache(ttl_seconds=0.03, stale_ttl_seconds=0.3, max_entries=8)
    key = ("example", "read", 1, None, ())
    await cache.set(key, _response(item_count=1))
    await asyncio.sleep(0.04)

    calls = 0

    async def refresher() -> ApiResponse:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _response(item_count=99)

    stale = await cache.get(key)
    assert stale.state == "stale"
    await cache.trigger_refresh(key, refresher)
    await asyncio.sleep(0.03)

    refreshed = await cache.get(key)
    assert refreshed.state == "fresh"
    assert refreshed.response is not None
    assert refreshed.response.metadata.item_count == 99
    assert calls == 1
