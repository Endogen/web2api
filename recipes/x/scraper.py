"""X (Twitter) scraper — retrieve recent posts from a user profile."""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Page

from web2api.scraper import BaseScraper, ScrapeResult


class Scraper(BaseScraper):
    """Scrape recent posts from an X.com user profile."""

    def supports(self, endpoint: str) -> bool:
        return endpoint == "posts"

    async def scrape(self, endpoint: str, page: Page, params: dict[str, Any]) -> ScrapeResult:
        username = (params.get("query") or "").strip().lstrip("@")
        if not username:
            raise RuntimeError("Missing username — pass q=<username>")

        count = min(int(params.get("count", "10")), 50)

        await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded")

        # Dismiss login prompts / cookie banners if they appear
        await self._dismiss_overlays(page)

        # Wait for tweets to load
        try:
            await page.wait_for_selector(
                '[data-testid="tweet"]',
                timeout=15000,
            )
        except Exception:
            # Check if we hit a login wall or the account doesn't exist
            content = await page.text_content("body") or ""
            if "This account doesn" in content or "doesn't exist" in content:
                raise RuntimeError(f"Account @{username} does not exist")
            if "These posts are protected" in content:
                raise RuntimeError(f"Account @{username} has protected posts")
            raise RuntimeError(
                f"Could not load posts for @{username} — "
                "X.com may be requiring login or blocking the request"
            )

        # Scroll to load more tweets if needed
        tweets = await self._extract_tweets(page)
        scroll_attempts = 0
        max_scrolls = 15

        while len(tweets) < count and scroll_attempts < max_scrolls:
            await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            await asyncio.sleep(1.5)
            await self._dismiss_overlays(page)
            new_tweets = await self._extract_tweets(page)
            if len(new_tweets) <= len(tweets):
                scroll_attempts += 1  # no new tweets loaded
            else:
                scroll_attempts = 0
            tweets = new_tweets

        return ScrapeResult(
            items=tweets[:count],
            current_page=1,
            has_next=len(tweets) > count,
        )

    @staticmethod
    async def _dismiss_overlays(page: Page) -> None:
        """Try to close login prompts and cookie banners."""
        # Close "Sign in" bottom sheet / modal
        for selector in [
            '[data-testid="sheetDialog"] [data-testid="app-bar-close"]',
            '[role="button"][aria-label="Close"]',
            'a[href="/i/flow/login"] + div [role="button"]',
        ]:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

    @staticmethod
    async def _extract_tweets(page: Page) -> list[dict[str, Any]]:
        """Extract tweet data from all visible tweet elements."""
        tweet_elements = await page.query_selector_all('[data-testid="tweet"]')
        tweets: list[dict[str, Any]] = []
        seen_texts: set[str] = set()

        for tweet_el in tweet_elements:
            try:
                tweet = await _parse_tweet(tweet_el, page)
                if tweet and tweet.get("text"):
                    # Deduplicate by text content
                    text_key = tweet["text"][:100]
                    if text_key not in seen_texts:
                        seen_texts.add(text_key)
                        tweets.append(tweet)
            except Exception:
                continue

        return tweets


async def _parse_tweet(tweet_el: Any, page: Page) -> dict[str, Any] | None:
    """Parse a single tweet element into a data dict."""
    # Tweet text
    text_el = await tweet_el.query_selector('[data-testid="tweetText"]')
    text = ""
    if text_el:
        text = (await text_el.text_content() or "").strip()

    if not text:
        return None

    # Author handle
    author = ""
    user_links = await tweet_el.query_selector_all('a[role="link"][href*="/"]')
    for link in user_links:
        href = await link.get_attribute("href") or ""
        if href.startswith("/") and href.count("/") == 1 and len(href) > 1:
            author = href.lstrip("/")
            break

    # Timestamp
    time_el = await tweet_el.query_selector("time")
    timestamp = ""
    if time_el:
        timestamp = await time_el.get_attribute("datetime") or ""

    # Engagement stats
    stats = await _extract_stats(tweet_el)

    # Tweet URL
    tweet_url = ""
    link_els = await tweet_el.query_selector_all('a[role="link"][href*="/status/"]')
    for link_el in link_els:
        href = await link_el.get_attribute("href") or ""
        if "/status/" in href:
            tweet_url = f"https://x.com{href}" if href.startswith("/") else href
            break

    return {
        "text": text,
        "author": author,
        "timestamp": timestamp,
        "url": tweet_url,
        **stats,
    }


async def _extract_stats(tweet_el: Any) -> dict[str, int | None]:
    """Extract engagement stats (replies, reposts, likes, views)."""
    stats: dict[str, int | None] = {
        "replies": None,
        "reposts": None,
        "likes": None,
        "views": None,
    }

    stat_map = {
        "reply": "replies",
        "retweet": "reposts",
        "like": "likes",
    }

    for testid, key in stat_map.items():
        el = await tweet_el.query_selector(f'[data-testid="{testid}"]')
        if el:
            aria = await el.get_attribute("aria-label") or ""
            # aria-label like "5 replies" or "123 Likes"
            import re
            match = re.search(r"(\d[\d,]*)", aria)
            if match:
                stats[key] = int(match.group(1).replace(",", ""))

    # Views (analytics)
    views_el = await tweet_el.query_selector('a[href*="/analytics"]')
    if views_el:
        aria = await views_el.get_attribute("aria-label") or ""
        import re
        match = re.search(r"(\d[\d,]*)", aria)
        if match:
            stats["views"] = int(match.group(1).replace(",", ""))

    return stats
