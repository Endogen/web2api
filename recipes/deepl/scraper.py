"""DeepL Translator scraper — German to English."""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Page

from web2api.scraper import BaseScraper, ScrapeResult


class Scraper(BaseScraper):
    """Translate German text to English via DeepL's web translator."""

    async def search(self, page: Page, params: dict[str, Any]) -> ScrapeResult:
        query = params.get("query") or ""
        if not query.strip():
            return ScrapeResult(items=[{"source_text": "", "translated_text": "", "source_lang": "de", "target_lang": "en"}])

        # Navigate to DeepL with German → English pre-selected
        await page.goto("https://www.deepl.com/en/translator#de/en/")
        
        # Wait for the source textarea to be ready
        source_area = await page.wait_for_selector(
            'd-textarea[data-testid="translator-source-input"]',
            timeout=15000,
        )
        if source_area is None:
            raise RuntimeError("Could not find DeepL source input")

        # Clear and type the source text
        await source_area.click()
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(query, delay=10)

        # Wait for translation to appear in the target area
        # DeepL populates the target textarea after a short delay
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
                # Fallback: try innerText
                text = await target_area.text_content()
            if text and text.strip() and text.strip() != query.strip():
                translated = text.strip()
                break
            # Also check for the paragraph inside the target
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
            items=[
                {
                    "source_text": query,
                    "translated_text": translated,
                    "source_lang": "de",
                    "target_lang": "en",
                }
            ],
        )
