"""Async Playwright browser context pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


@dataclass(slots=True)
class _ContextSlot:
    """Internal container for pooled browser contexts."""

    slot_id: int
    context: BrowserContext
    use_count: int = 0


class BrowserPool:
    """Manage a shared browser with reusable contexts for concurrent requests."""

    def __init__(
        self,
        *,
        max_contexts: int = 5,
        context_ttl: int = 50,
        acquire_timeout: float = 30.0,
        page_timeout_ms: int = 15_000,
        queue_size: int = 20,
        headless: bool = True,
    ) -> None:
        self.max_contexts = max_contexts
        self.context_ttl = context_ttl
        self.acquire_timeout = acquire_timeout
        self.page_timeout_ms = page_timeout_ms
        self.queue_size = queue_size
        self.headless = headless

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context_queue: asyncio.Queue[_ContextSlot] | None = None
        self._active_pages: dict[int, tuple[Page, _ContextSlot]] = {}
        self._pending_waiters = 0
        self._total_requests_served = 0
        self._state_lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch Chromium and initialize pooled browser contexts."""
        async with self._state_lock:
            if self._browser is not None and self._browser.is_connected():
                return

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._context_queue = asyncio.Queue(maxsize=self.max_contexts)
            for slot_id in range(self.max_contexts):
                context = await self._browser.new_context()
                await self._context_queue.put(_ContextSlot(slot_id=slot_id, context=context))

    async def stop(self) -> None:
        """Close pooled contexts and the browser process."""
        async with self._state_lock:
            context_queue = self._context_queue
            active_pages = list(self._active_pages.values())
            browser = self._browser
            playwright = self._playwright

            self._active_pages.clear()
            self._context_queue = None
            self._browser = None
            self._playwright = None
            self._pending_waiters = 0

        contexts: dict[int, BrowserContext] = {}
        if context_queue is not None:
            while True:
                try:
                    slot = context_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                contexts[slot.slot_id] = slot.context

        for page, slot in active_pages:
            with suppress(Exception):
                await page.close()
            contexts[slot.slot_id] = slot.context

        for context in contexts.values():
            with suppress(Exception):
                await context.close()

        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()

    async def acquire(self, timeout: float | None = None) -> Page:
        """Acquire a page from the pool, waiting until a context is available."""
        context_queue = self._context_queue
        if context_queue is None:
            raise RuntimeError("browser pool is not started")
        if self._browser is None or not self._browser.is_connected():
            raise RuntimeError("browser is not connected")

        effective_timeout = self.acquire_timeout if timeout is None else timeout
        async with self._state_lock:
            if self._pending_waiters >= self.queue_size:
                raise TimeoutError("browser pool queue is full")
            self._pending_waiters += 1

        try:
            slot = await asyncio.wait_for(context_queue.get(), timeout=effective_timeout)
        except TimeoutError as exc:
            raise TimeoutError("timed out waiting for a browser context") from exc
        finally:
            async with self._state_lock:
                self._pending_waiters -= 1

        try:
            page = await slot.context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            await self._replace_slot(slot)
            raise RuntimeError("failed to create page from browser context") from exc

        async with self._state_lock:
            self._active_pages[id(page)] = (page, slot)
            self._total_requests_served += 1
        return page

    async def release(self, page: Page) -> None:
        """Release a page back to the pool after use."""
        async with self._state_lock:
            page_state = self._active_pages.pop(id(page), None)
            context_queue = self._context_queue

        if page_state is None:
            with suppress(Exception):
                await page.close()
            return

        _, slot = page_state
        corrupted = False
        with suppress(Exception):
            await page.close()

        try:
            await slot.context.clear_cookies()
        except Exception:  # noqa: BLE001
            corrupted = True

        slot.use_count += 1
        if corrupted or slot.use_count >= self.context_ttl:
            slot = await self._recreate_slot(slot)

        if context_queue is None:
            with suppress(Exception):
                await slot.context.close()
            return
        await context_queue.put(slot)

    @asynccontextmanager
    async def page(self, timeout: float | None = None) -> AsyncGenerator[Page, None]:
        """Acquire and automatically release a page."""
        page = await self.acquire(timeout=timeout)
        try:
            yield page
        finally:
            await self.release(page)

    @property
    def health(self) -> dict[str, int | bool]:
        """Return current browser pool health details."""
        browser_connected = self._browser is not None and self._browser.is_connected()
        available_contexts = self._context_queue.qsize() if self._context_queue is not None else 0
        total_contexts = self.max_contexts if browser_connected else 0
        return {
            "browser_connected": browser_connected,
            "total_contexts": total_contexts,
            "available_contexts": available_contexts,
            "queue_size": self._pending_waiters,
            "total_requests_served": self._total_requests_served,
        }

    async def _replace_slot(self, slot: _ContextSlot) -> None:
        context_queue = self._context_queue
        if self._browser is None or context_queue is None:
            with suppress(Exception):
                await slot.context.close()
            return

        with suppress(Exception):
            await slot.context.close()
        new_context = await self._browser.new_context()
        await context_queue.put(_ContextSlot(slot_id=slot.slot_id, context=new_context))

    async def _recreate_slot(self, slot: _ContextSlot) -> _ContextSlot:
        if self._browser is None:
            with suppress(Exception):
                await slot.context.close()
            raise RuntimeError("browser pool is not started")

        with suppress(Exception):
            await slot.context.close()
        new_context = await self._browser.new_context()
        return _ContextSlot(slot_id=slot.slot_id, context=new_context)
