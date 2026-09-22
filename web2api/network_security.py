"""Outbound network guards for browser-backed and custom recipes."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import BrowserContext

from web2api.settings import env_bool

AddressResolver = Callable[..., list[tuple[Any, ...]]]


class UnsafeOutboundURL(ValueError):
    """Raised when an outbound URL could reach a non-public network target."""


def private_network_access_enabled() -> bool:
    """Return whether the explicit private-network escape hatch is enabled."""
    return env_bool("WEB2API_ALLOW_PRIVATE_NETWORK", default=False)


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
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeOutboundURL("outbound URL is malformed") from exc
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


def validate_public_websocket_url(
    url: str,
    *,
    allow_private_network: bool = False,
    resolver: AddressResolver = socket.getaddrinfo,
) -> str:
    """Validate a WebSocket URL using the same host policy as HTTP requests."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeOutboundURL("WebSocket URL is malformed") from exc
    if parsed.scheme not in {"ws", "wss"}:
        raise UnsafeOutboundURL("only ws and wss WebSocket URLs are allowed")
    http_scheme = "https" if parsed.scheme == "wss" else "http"
    http_url = urlunsplit(
        (http_scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
    validate_public_http_url(
        http_url,
        allow_private_network=allow_private_network,
        resolver=resolver,
    )
    return url


async def install_public_network_guard(
    context: BrowserContext,
    *,
    allow_private_network: bool = False,
) -> None:
    """Guard HTTP and WebSocket traffic for every page in a browser context."""

    async def _guard(route: Any, request: Any) -> None:
        url = str(request.url)
        scheme = urlsplit(url).scheme.lower()
        if scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        if scheme not in {"http", "https"}:
            await route.abort("blockedbyclient")
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
        if allow_private_network:
            await route.continue_()
            return

        # Chromium does not necessarily re-run route handlers for a redirect
        # followed by the network stack. Fetch one hop, validate Location, and
        # fulfill the hop so no unvalidated redirect can reach the browser.
        try:
            response = await route.fetch(max_redirects=0)
        except Exception:  # noqa: BLE001 - a failed fetch must settle the route
            await route.abort("failed")
            return
        try:
            location = response.headers.get("location")
            if location:
                redirect_url = urljoin(url, location)
                try:
                    await asyncio.to_thread(
                        validate_public_http_url,
                        redirect_url,
                        allow_private_network=False,
                    )
                except UnsafeOutboundURL:
                    await route.abort("blockedbyclient")
                    return
            await route.fulfill(response=response)
        finally:
            await response.dispose()

    async def _guard_websocket(web_socket: Any) -> None:
        try:
            await asyncio.to_thread(
                validate_public_websocket_url,
                str(web_socket.url),
                allow_private_network=allow_private_network,
            )
        except UnsafeOutboundURL:
            await web_socket.close(code=1008, reason="blocked by outbound network policy")
            return
        web_socket.connect_to_server()

    await context.route("**/*", _guard)
    await context.route_web_socket("**/*", _guard_websocket)
