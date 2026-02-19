"""Integration tests for the browser pool."""

from __future__ import annotations

import asyncio

import pytest

from web2api.pool import BrowserPool


class FakePage:
    """Minimal page stub used by pool tests."""

    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.default_timeout: int | None = None
        self.closed = False

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.default_timeout = timeout_ms

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    """Minimal browser context stub used by pool tests."""

    def __init__(self, context_id: int) -> None:
        self.context_id = context_id
        self.closed = False
        self.clear_calls = 0
        self.fail_clear = False

    async def new_page(self) -> FakePage:
        if self.closed:
            raise RuntimeError("context is closed")
        return FakePage(self)

    async def clear_cookies(self) -> None:
        if self.fail_clear:
            raise RuntimeError("context is corrupted")
        self.clear_calls += 1

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Minimal browser stub used by pool tests."""

    def __init__(self) -> None:
        self._connected = True
        self._next_context_id = 1
        self.created_context_ids: list[int] = []

    async def new_context(self) -> FakeContext:
        context = FakeContext(self._next_context_id)
        self._next_context_id += 1
        self.created_context_ids.append(context.context_id)
        return context

    async def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


class FakeChromium:
    """Chromium launcher stub returning a fake browser."""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    async def launch(self, *, headless: bool) -> FakeBrowser:
        _ = headless
        return self._browser


class FakePlaywright:
    """Playwright stub with chromium launcher and stop lifecycle."""

    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    """Object matching ``async_playwright().start()`` usage."""

    def __init__(self, playwright: FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> FakePlaywright:
        return self._playwright


def _patch_playwright(monkeypatch: pytest.MonkeyPatch) -> FakeBrowser:
    fake_browser = FakeBrowser()
    fake_playwright = FakePlaywright(fake_browser)
    starter = FakePlaywrightStarter(fake_playwright)
    monkeypatch.setattr("web2api.pool.async_playwright", lambda: starter)
    return fake_browser


@pytest.mark.asyncio
async def test_pool_acquire_release_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_playwright(monkeypatch)
    pool = BrowserPool(max_contexts=1)
    await pool.start()

    page = await pool.acquire()
    assert page.default_timeout == 15_000
    assert pool.health["available_contexts"] == 0
    assert pool.health["total_requests_served"] == 1

    await pool.release(page)
    assert pool.health["available_contexts"] == 1

    await pool.stop()


@pytest.mark.asyncio
async def test_pool_concurrency_respects_context_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_playwright(monkeypatch)
    pool = BrowserPool(max_contexts=2, acquire_timeout=1.0)
    await pool.start()

    page_1 = await pool.acquire()
    page_2 = await pool.acquire()

    acquire_task = asyncio.create_task(pool.acquire(timeout=1.0))
    await asyncio.sleep(0.01)
    assert pool.health["queue_size"] == 1

    await pool.release(page_1)
    page_3 = await acquire_task

    await pool.release(page_2)
    await pool.release(page_3)
    await pool.stop()


@pytest.mark.asyncio
async def test_pool_times_out_when_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_playwright(monkeypatch)
    pool = BrowserPool(max_contexts=1, acquire_timeout=0.01)
    await pool.start()

    page = await pool.acquire()
    with pytest.raises(TimeoutError):
        await pool.acquire(timeout=0.01)

    await pool.release(page)
    await pool.stop()


@pytest.mark.asyncio
async def test_pool_recycles_context_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_playwright(monkeypatch)
    pool = BrowserPool(max_contexts=1, context_ttl=1)
    await pool.start()

    page_1 = await pool.acquire()
    first_context_id = page_1.context.context_id
    await pool.release(page_1)

    page_2 = await pool.acquire()
    second_context_id = page_2.context.context_id

    assert second_context_id != first_context_id

    await pool.release(page_2)
    await pool.stop()


@pytest.mark.asyncio
async def test_pool_health_reports_expected_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_playwright(monkeypatch)
    pool = BrowserPool(max_contexts=2)
    await pool.start()

    assert pool.health == {
        "browser_connected": True,
        "total_contexts": 2,
        "available_contexts": 2,
        "queue_size": 0,
        "total_requests_served": 0,
    }

    page = await pool.acquire()
    assert pool.health["available_contexts"] == 1
    assert pool.health["total_requests_served"] == 1

    await pool.release(page)
    await pool.stop()


@pytest.mark.asyncio
async def test_pool_waiters_do_not_go_negative_when_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_playwright(monkeypatch)
    pool = BrowserPool(max_contexts=1, acquire_timeout=0.05)
    await pool.start()

    page = await pool.acquire()
    waiter = asyncio.create_task(pool.acquire(timeout=0.05))
    await asyncio.sleep(0.01)
    assert pool.health["queue_size"] == 1

    await pool.stop()
    with pytest.raises(TimeoutError):
        await waiter

    assert pool.health["queue_size"] == 0
    await pool.release(page)
    assert page.closed is True
