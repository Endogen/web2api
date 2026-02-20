"""Async Playwright browser context pool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from web2api.logging_utils import log_event

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_CONTEXT_OPTS: dict = {
    "user_agent": _DEFAULT_UA,
    "viewport": {"width": 1920, "height": 1080},
    "locale": "en-US",
}


@dataclass(slots=True)
class _ContextSlot:
    """Internal container for pooled browser contexts."""

    slot_id: int
    context: BrowserContext
    use_count: int = 0


logger = logging.getLogger(__name__)


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
        """Configure browser pool limits, recycling behavior, and launch options."""
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
        log_event(
            logger,
            logging.INFO,
            "browser_pool.starting",
            max_contexts=self.max_contexts,
            context_ttl=self.context_ttl,
            headless=self.headless,
        )
        async with self._state_lock:
            if self._browser is not None and self._browser.is_connected():
                log_event(
                    logger,
                    logging.DEBUG,
                    "browser_pool.start_skipped",
                    reason="already_running",
                )
                return

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._context_queue = asyncio.Queue(maxsize=self.max_contexts)
            for slot_id in range(self.max_contexts):
                context = await self._browser.new_context(**_CONTEXT_OPTS)
                await self._context_queue.put(_ContextSlot(slot_id=slot_id, context=context))
        log_event(
            logger,
            logging.INFO,
            "browser_pool.started",
            max_contexts=self.max_contexts,
            available_contexts=self.max_contexts,
        )

    async def stop(self) -> None:
        """Close pooled contexts and the browser process."""
        log_event(logger, logging.INFO, "browser_pool.stopping")
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
            try:
                await asyncio.wait_for(page.close(), timeout=5.0)
            except TimeoutError:
                log_event(
                    logger,
                    logging.WARNING,
                    "browser_pool.page_close_timeout",
                    slot_id=slot.slot_id,
                )
            except Exception:  # noqa: BLE001
                pass
            contexts[slot.slot_id] = slot.context

        for ctx_slot_id, context in contexts.items():
            try:
                await asyncio.wait_for(context.close(), timeout=5.0)
            except TimeoutError:
                log_event(
                    logger,
                    logging.WARNING,
                    "browser_pool.context_close_timeout",
                    slot_id=ctx_slot_id,
                )
            except Exception:  # noqa: BLE001
                pass

        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()
        log_event(
            logger,
            logging.INFO,
            "browser_pool.stopped",
            released_contexts=len(contexts),
            released_pages=len(active_pages),
        )

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
                log_event(
                    logger,
                    logging.WARNING,
                    "browser_pool.acquire_rejected",
                    reason="queue_full",
                    queue_size=self.queue_size,
                )
                raise TimeoutError("browser pool queue is full")
            self._pending_waiters += 1

        try:
            slot = await asyncio.wait_for(context_queue.get(), timeout=effective_timeout)
        except TimeoutError as exc:
            log_event(
                logger,
                logging.WARNING,
                "browser_pool.acquire_timeout",
                timeout_seconds=effective_timeout,
            )
            raise TimeoutError("timed out waiting for a browser context") from exc
        finally:
            async with self._state_lock:
                if self._pending_waiters > 0:
                    self._pending_waiters -= 1

        try:
            page = await slot.context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            await self._replace_slot(slot)
            log_event(
                logger,
                logging.ERROR,
                "browser_pool.page_create_failed",
                slot_id=slot.slot_id,
                error=str(exc),
                exc_info=exc,
            )
            raise RuntimeError("failed to create page from browser context") from exc

        async with self._state_lock:
            self._active_pages[id(page)] = (page, slot)
            self._total_requests_served += 1
        log_event(
            logger,
            logging.DEBUG,
            "browser_pool.page_acquired",
            slot_id=slot.slot_id,
            pending_waiters=self._pending_waiters,
        )
        return page

    async def release(self, page: Page) -> None:
        """Release a page back to the pool after use."""
        async with self._state_lock:
            page_state = self._active_pages.pop(id(page), None)
            context_queue = self._context_queue

        if page_state is None:
            log_event(logger, logging.WARNING, "browser_pool.release_unknown_page")
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
            reason = "corrupted" if corrupted else "ttl_expired"
            log_event(
                logger,
                logging.INFO,
                "browser_pool.context_recycled",
                slot_id=slot.slot_id,
                reason=reason,
            )
            slot = await self._recreate_slot(slot)

        if context_queue is None:
            with suppress(Exception):
                await slot.context.close()
            return
        await context_queue.put(slot)
        log_event(
            logger,
            logging.DEBUG,
            "browser_pool.page_released",
            slot_id=slot.slot_id,
            use_count=slot.use_count,
        )

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
        new_context = await self._browser.new_context(**_CONTEXT_OPTS)
        await context_queue.put(_ContextSlot(slot_id=slot.slot_id, context=new_context))
        log_event(logger, logging.INFO, "browser_pool.context_replaced", slot_id=slot.slot_id)

    async def _recreate_slot(self, slot: _ContextSlot) -> _ContextSlot:
        if self._browser is None:
            with suppress(Exception):
                await slot.context.close()
            raise RuntimeError("browser pool is not started")

        with suppress(Exception):
            await slot.context.close()
        new_context = await self._browser.new_context(**_CONTEXT_OPTS)
        log_event(logger, logging.INFO, "browser_pool.context_recreated", slot_id=slot.slot_id)
        return _ContextSlot(slot_id=slot.slot_id, context=new_context)
