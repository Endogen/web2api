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

        translated = ""
        for _ in range(30):  # up to 15 seconds
            await asyncio.sleep(0.5)
            target_area = await page.query_selector(
                'd-textarea[data-testid="translator-target-input"]'
            )
            if target_area is None:
                continue
            text = await target_area.get_attribute("value")
            if not text:
                text = await target_area.text_content()
            if text and text.strip() and text.strip() != query.strip():
                translated = text.strip()
                break
            target_p = await page.query_selector(
                '[data-testid="translator-target-input"] p'
            )
            if target_p:
                text = await target_p.text_content()
                if text and text.strip():
                    translated = text.strip()
                    break

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
