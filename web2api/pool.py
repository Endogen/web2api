"""Lazy Playwright browser pool with isolated per-request contexts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from web2api.logging_utils import log_event
from web2api.network_security import install_public_network_guard

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_CONTEXT_OPTS: dict = {
    "user_agent": _DEFAULT_UA,
    "viewport": {"width": 1920, "height": 1080},
    "locale": "en-US",
    "service_workers": "block",
}

logger = logging.getLogger(__name__)


class BrowserPool:
    """Manage one lazy browser and fresh isolated contexts per request."""

    def __init__(
        self,
        *,
        max_contexts: int = 5,
        acquire_timeout: float = 30.0,
        page_timeout_ms: int = 15_000,
        queue_size: int = 20,
        headless: bool = True,
        proxy: str | None = None,
    ) -> None:
        """Configure browser concurrency, queueing, and launch options."""
        if max_contexts < 1:
            raise ValueError("max_contexts must be >= 1")
        if queue_size < 0:
            raise ValueError("queue_size must be >= 0")
        self.max_contexts = max_contexts
        self.acquire_timeout = acquire_timeout
        self.page_timeout_ms = page_timeout_ms
        self.queue_size = queue_size
        self.headless = headless
        self.proxy = proxy

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._capacity = asyncio.Semaphore(max_contexts)
        self._active_pages: dict[int, tuple[Page, BrowserContext]] = {}
        self._pending_waiters = 0
        self._total_requests_served = 0
        self._state_lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch Chromium if it has not been needed yet."""
        async with self._state_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            log_event(
                logger,
                logging.INFO,
                "browser_pool.starting",
                max_contexts=self.max_contexts,
                headless=self.headless,
            )
            playwright = await async_playwright().start()
            try:
                launch_options: dict = {
                    "headless": self.headless,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if self.proxy:
                    launch_options["proxy"] = {"server": self.proxy}
                browser = await playwright.chromium.launch(
                    **launch_options,
                )
            except Exception:
                with suppress(Exception):
                    await playwright.stop()
                raise
            self._playwright = playwright
            self._browser = browser
        log_event(logger, logging.INFO, "browser_pool.started")

    async def stop(self) -> None:
        """Close active contexts and the browser process."""
        log_event(logger, logging.INFO, "browser_pool.stopping")
        async with self._state_lock:
            active_pages = list(self._active_pages.values())
            self._active_pages.clear()
            browser = self._browser
            playwright = self._playwright
            self._browser = None
            self._playwright = None
            self._pending_waiters = 0

        for page, context in active_pages:
            with suppress(Exception):
                await asyncio.wait_for(page.close(), timeout=5.0)
            with suppress(Exception):
                await asyncio.wait_for(context.close(), timeout=5.0)
            self._capacity.release()

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
            released_contexts=len(active_pages),
        )

    async def acquire(
        self,
        timeout: float | None = None,
        *,
        allow_private_network: bool = False,
    ) -> Page:
        """Acquire capacity and create a fresh guarded browser context and page."""
        await self.start()
        effective_timeout = self.acquire_timeout if timeout is None else timeout

        async with self._state_lock:
            if self._pending_waiters >= self.queue_size and self._capacity.locked():
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
            await asyncio.wait_for(self._capacity.acquire(), timeout=effective_timeout)
        except TimeoutError as exc:
            log_event(
                logger,
                logging.WARNING,
                "browser_pool.acquire_timeout",
                timeout_seconds=effective_timeout,
            )
            raise TimeoutError("timed out waiting for browser capacity") from exc
        finally:
            async with self._state_lock:
                self._pending_waiters = max(0, self._pending_waiters - 1)

        context: BrowserContext | None = None
        try:
            async with self._state_lock:
                browser = self._browser
                if browser is None or not browser.is_connected():
                    raise RuntimeError("browser is not connected")
            context = await browser.new_context(**_CONTEXT_OPTS)
            await install_public_network_guard(
                context,
                allow_private_network=allow_private_network,
            )
            page = await context.new_page()
            page.set_default_timeout(self.page_timeout_ms)
        except Exception as exc:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            self._capacity.release()
            log_event(
                logger,
                logging.ERROR,
                "browser_pool.page_create_failed",
                error=str(exc),
                exc_info=exc,
            )
            raise RuntimeError("failed to create isolated browser page") from exc

        async with self._state_lock:
            self._active_pages[id(page)] = (page, context)
            self._total_requests_served += 1
        return page

    async def release(self, page: Page) -> None:
        """Close a page and its entire isolated context, then return capacity."""
        async with self._state_lock:
            page_state = self._active_pages.pop(id(page), None)

        if page_state is None:
            log_event(logger, logging.WARNING, "browser_pool.release_unknown_page")
            with suppress(Exception):
                await page.close()
            return

        _, context = page_state
        with suppress(Exception):
            await page.close()
        with suppress(Exception):
            await context.close()
        self._capacity.release()

    @asynccontextmanager
    async def page(
        self,
        timeout: float | None = None,
        *,
        allow_private_network: bool = False,
    ) -> AsyncGenerator[Page, None]:
        """Acquire and automatically dispose of an isolated page context."""
        page = await self.acquire(
            timeout=timeout,
            allow_private_network=allow_private_network,
        )
        try:
            yield page
        finally:
            await self.release(page)

    @property
    def health(self) -> dict[str, int | bool]:
        """Return current browser and capacity metrics."""
        browser_started = self._browser is not None
        browser_connected = bool(
            self._browser is not None and self._browser.is_connected()
        )
        active_contexts = len(self._active_pages)
        return {
            "browser_started": browser_started,
            "browser_connected": browser_connected,
            "ready": not browser_started or browser_connected,
            "total_contexts": self.max_contexts,
            "available_contexts": max(0, self.max_contexts - active_contexts),
            "queue_size": self._pending_waiters,
            "total_requests_served": self._total_requests_served,
        }
