"""In-memory API response cache with stale-while-revalidate semantics."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from web2api.schemas import ApiResponse

type CacheKey = tuple[str, str, int, str | None, tuple[tuple[str, str], ...]]
LookupState = Literal["miss", "fresh", "stale"]


@dataclass(slots=True)
class CacheLookup:
    """Lookup result for a cache key."""

    state: LookupState
    response: ApiResponse | None = None


@dataclass(slots=True)
class _CacheEntry:
    response: ApiResponse
    expires_at: float
    stale_until: float
    refreshing: bool = False


class ResponseCache:
    """Store successful scrape responses keyed by request parameters."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        stale_ttl_seconds: float = 120.0,
        max_entries: int = 500,
    ) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self.stale_ttl_seconds = max(0.0, stale_ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._stale_hits = 0
        self._misses = 0
        self._stores = 0
        self._evictions = 0
        self._refresh_tasks: set[asyncio.Task[None]] = set()

    async def get(self, key: CacheKey) -> CacheLookup:
        """Look up a cache entry and classify it as fresh/stale/miss."""
        now = monotonic()
        async with self._lock:
            self._purge_expired_unlocked(now)
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return CacheLookup(state="miss")

            self._entries.move_to_end(key)
            if entry.expires_at > now:
                self._hits += 1
                return CacheLookup(state="fresh", response=entry.response.model_copy(deep=True))

            self._stale_hits += 1
            return CacheLookup(state="stale", response=entry.response.model_copy(deep=True))

    async def set(self, key: CacheKey, response: ApiResponse) -> None:
        """Insert or replace a successful response in cache."""
        if self.ttl_seconds <= 0 or response.error is not None:
            return

        now = monotonic()
        entry = _CacheEntry(
            response=response.model_copy(deep=True),
            expires_at=now + self.ttl_seconds,
            stale_until=now + self.ttl_seconds + self.stale_ttl_seconds,
        )
        async with self._lock:
            self._purge_expired_unlocked(now)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._stores += 1
            self._trim_to_capacity_unlocked()

    async def trigger_refresh(
        self,
        key: CacheKey,
        refresher: Callable[[], Awaitable[ApiResponse]],
    ) -> None:
        """Refresh a stale key in the background if not already refreshing."""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.refreshing:
                return
            entry.refreshing = True

        task = asyncio.create_task(self._run_refresh(key, refresher))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def stats(self) -> dict[str, int | float | bool]:
        """Return cache health and counters for diagnostics."""
        now = monotonic()
        async with self._lock:
            self._purge_expired_unlocked(now)
            return {
                "enabled": True,
                "ttl_seconds": self.ttl_seconds,
                "stale_ttl_seconds": self.stale_ttl_seconds,
                "max_entries": self.max_entries,
                "entries": len(self._entries),
                "hits": self._hits,
                "stale_hits": self._stale_hits,
                "misses": self._misses,
                "stores": self._stores,
                "evictions": self._evictions,
                "refresh_tasks": len(self._refresh_tasks),
            }

    async def _run_refresh(
        self,
        key: CacheKey,
        refresher: Callable[[], Awaitable[ApiResponse]],
    ) -> None:
        try:
            refreshed = await refresher()
            if refreshed.error is None:
                await self.set(key, refreshed)
        finally:
            async with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    entry.refreshing = False

    def _purge_expired_unlocked(self, now: float) -> None:
        expired_keys = [key for key, entry in self._entries.items() if entry.stale_until <= now]
        for key in expired_keys:
            self._entries.pop(key, None)
            self._evictions += 1

    def _trim_to_capacity_unlocked(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1
