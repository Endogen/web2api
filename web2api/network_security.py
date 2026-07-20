"""Outbound network guards for browser-backed and custom recipes."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page

AddressResolver = Callable[..., list[tuple[Any, ...]]]


class UnsafeOutboundURL(ValueError):
    """Raised when an outbound URL could reach a non-public network target."""


def private_network_access_enabled() -> bool:
    """Return whether the explicit private-network escape hatch is enabled."""
    return os.environ.get("WEB2API_ALLOW_PRIVATE_NETWORK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def validate_httpx_request(request: Any) -> None:
    """httpx request hook that validates initial and redirect destinations."""
    await asyncio.to_thread(
        validate_public_http_url,
        str(request.url),
        allow_private_network=private_network_access_enabled(),
    )


def validate_public_http_url(
    url: str,
    *,
    allow_private_network: bool = False,
    resolver: AddressResolver = socket.getaddrinfo,
) -> str:
    """Validate an HTTP(S) URL and reject local or non-global destinations."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeOutboundURL("only http and https outbound URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundURL("outbound URLs must not contain credentials")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeOutboundURL("outbound URL must include a hostname")
    normalized_host = hostname.rstrip(".").lower()
    if allow_private_network:
        return url
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise UnsafeOutboundURL("local network destinations are blocked")

    try:
        literal = ipaddress.ip_address(normalized_host)
        addresses = [literal]
    except ValueError:
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise UnsafeOutboundURL("outbound URL contains an invalid port") from exc
        try:
            resolved = resolver(normalized_host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeOutboundURL(f"could not resolve outbound hostname: {hostname}") from exc
        addresses = []
        for entry in resolved:
            raw_address = entry[4][0]
            try:
                addresses.append(ipaddress.ip_address(raw_address))
            except ValueError:
                continue

    if not addresses:
        raise UnsafeOutboundURL(f"could not resolve outbound hostname: {hostname}")
    if any(not address.is_global for address in addresses):
        raise UnsafeOutboundURL("local, private, reserved, and link-local destinations are blocked")
    return url


async def install_public_network_guard(
    page: Page,
    *,
    allow_private_network: bool = False,
) -> None:
    """Guard every Playwright HTTP request, including redirect targets."""

    async def _guard(route: Any, request: Any) -> None:
        url = str(request.url)
        if not url.startswith(("http://", "https://")):
            await route.continue_()
            return
        try:
            await asyncio.to_thread(
                validate_public_http_url,
                url,
                allow_private_network=allow_private_network,
            )
        except UnsafeOutboundURL:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    await page.route("**/*", _guard)
