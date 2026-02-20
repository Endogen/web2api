"""DeepL Translator scraper — supports multiple language pairs."""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Page

from web2api.scraper import BaseScraper, ScrapeResult

# Map endpoint names to (source_lang, target_lang) pairs
_LANG_PAIRS: dict[str, tuple[str, str]] = {
    "de-en": ("de", "en"),
    "en-de": ("en", "de"),
}


class Scraper(BaseScraper):
    """Translate text via DeepL's web translator."""

    def supports(self, endpoint: str) -> bool:
        return endpoint in _LANG_PAIRS

    async def scrape(self, endpoint: str, page: Page, params: dict[str, Any]) -> ScrapeResult:
        source_lang, target_lang = _LANG_PAIRS[endpoint]
        query = params.get("query") or ""

        if not query.strip():
            return ScrapeResult(
                items=[{
                    "source_text": "",
                    "translated_text": "",
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                }]
            )

        await page.goto(f"https://www.deepl.com/en/translator#{source_lang}/{target_lang}/")

        source_area = await page.wait_for_selector(
            'd-textarea[data-testid="translator-source-input"]',
            timeout=15000,
        )
        if source_area is None:
            raise RuntimeError("Could not find DeepL source input")

        await source_area.click()
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(query, delay=10)

        # Wait for translation to appear and stabilize.
        # DeepL streams results progressively, so we wait until the
        # target text stops changing for a few consecutive checks.
        translated = ""
        stable_count = 0
        required_stable = 6  # must be unchanged for 6 consecutive checks (3s)

        for _ in range(80):  # up to 40 seconds total
            await asyncio.sleep(0.5)
            current = await self._read_target(page)

            if not current or current == query.strip():
                stable_count = 0
                continue

            if current == translated:
                stable_count += 1
                if stable_count >= required_stable:
                    break
            else:
                translated = current
                stable_count = 0

        if not translated:
            raise RuntimeError("Translation did not appear within timeout")

        return ScrapeResult(
            items=[{
                "source_text": query,
                "translated_text": translated,
                "source_lang": source_lang,
                "target_lang": target_lang,
            }],
        )

    @staticmethod
    async def _read_target(page: Page) -> str:
        """Extract the current translation text from the target area."""
        # Try the value attribute first
        target_area = await page.query_selector(
            'd-textarea[data-testid="translator-target-input"]'
        )
        if target_area is not None:
            text = await target_area.get_attribute("value")
            if text and text.strip():
                return text.strip()
            text = await target_area.text_content()
            if text and text.strip():
                return text.strip()

        # Fallback: paragraph inside the target
        target_p = await page.query_selector(
            '[data-testid="translator-target-input"] p'
        )
        if target_p is not None:
            text = await target_p.text_content()
            if text and text.strip():
                return text.strip()

        return ""
