"""X (Twitter) scraper — uses bird CLI for authenticated API access."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from playwright.async_api import Page

from web2api.scraper import BaseScraper, ScrapeResult

# Auth tokens read from ~/.bird_auth or environment
_AUTH_TOKEN = os.environ.get("BIRD_AUTH_TOKEN", "")
_CT0 = os.environ.get("BIRD_CT0", "")


def _load_auth() -> tuple[str, str]:
    """Load bird auth tokens from env or ~/.bird_auth file."""
    auth_token = _AUTH_TOKEN
    ct0 = _CT0
    if auth_token and ct0:
        return auth_token, ct0

    bird_auth_path = os.path.expanduser("~/.bird_auth")
    if os.path.exists(bird_auth_path):
        with open(bird_auth_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("AUTH_TOKEN="):
                    auth_token = line.split("=", 1)[1]
                elif line.startswith("CT0="):
                    ct0 = line.split("=", 1)[1]

    if not auth_token or not ct0:
        raise RuntimeError(
            "Missing X/Twitter credentials. "
            "Set BIRD_AUTH_TOKEN + BIRD_CT0 env vars or create ~/.bird_auth"
        )
    return auth_token, ct0


class Scraper(BaseScraper):
    """Fetch user tweets via the bird CLI."""

    def supports(self, endpoint: str) -> bool:
        return endpoint == "posts"

    async def scrape(self, endpoint: str, page: Page, params: dict[str, Any]) -> ScrapeResult:
        username = (params.get("query") or "").strip().lstrip("@")
        if not username:
            raise RuntimeError("Missing username — pass q=<username>")

        count = min(int(params.get("count", "10")), 50)
        auth_token, ct0 = _load_auth()

        # Shell out to bird CLI
        cmd = [
            "bird", "user-tweets", username,
            "-n", str(count),
            "--json",
            "--auth-token", auth_token,
            "--ct0", ct0,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            if "Could not find user" in error_msg or "not found" in error_msg.lower():
                raise RuntimeError(f"Account @{username} not found")
            raise RuntimeError(f"bird CLI failed: {error_msg}")

        # Parse JSON output — bird prints info lines to stderr, JSON to stdout
        raw_output = stdout.decode().strip()

        # Find the JSON array in the output (skip any non-JSON lines)
        json_start = raw_output.find("[")
        if json_start == -1:
            raise RuntimeError(f"No JSON output from bird CLI for @{username}")

        tweets_data = json.loads(raw_output[json_start:])

        items: list[dict[str, Any]] = []
        for tweet in tweets_data[:count]:
            author_username = tweet.get("author", {}).get("username", username)
            items.append({
                "text": tweet.get("text", ""),
                "author": author_username,
                "author_name": tweet.get("author", {}).get("name", ""),
                "timestamp": tweet.get("createdAt", ""),
                "url": f"https://x.com/{author_username}/status/{tweet.get('id', '')}",
                "replies": tweet.get("replyCount"),
                "reposts": tweet.get("retweetCount"),
                "likes": tweet.get("likeCount"),
                "views": tweet.get("viewCount"),
                "is_retweet": tweet.get("text", "").startswith("RT @"),
            })

        return ScrapeResult(
            items=items,
            current_page=1,
            has_next=len(tweets_data) > count,
        )
