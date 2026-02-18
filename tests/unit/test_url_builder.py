"""Unit tests for URL template construction."""

from __future__ import annotations

from web2api.config import EndpointConfig
from web2api.engine import build_url


def _endpoint(url: str, *, start: int = 1) -> EndpointConfig:
    return EndpointConfig.model_validate(
        {
            "url": url,
            "items": {
                "container": ".item",
                "fields": {
                    "title": {"selector": ".title"},
                },
            },
            "pagination": {
                "type": "page_param",
                "param": "page",
                "start": start,
            },
        }
    )


def test_page_placeholder_uses_recipe_start_offset() -> None:
    endpoint = _endpoint("https://example.com/items?page={page}", start=0)

    assert build_url(endpoint, page=1) == "https://example.com/items?page=0"
    assert build_url(endpoint, page=3) == "https://example.com/items?page=2"


def test_page_zero_placeholder_is_zero_indexed_from_api_page() -> None:
    endpoint = _endpoint("https://example.com/items?page={page_zero}", start=1)

    assert build_url(endpoint, page=1) == "https://example.com/items?page=0"
    assert build_url(endpoint, page=2) == "https://example.com/items?page=1"


def test_query_placeholder_is_url_encoded() -> None:
    endpoint = _endpoint("https://example.com/search?q={query}&p={page}", start=1)

    assert (
        build_url(endpoint, page=2, query="python tips")
        == "https://example.com/search?q=python+tips&p=2"
    )
