"""Tests for outbound URL validation."""

from __future__ import annotations

import socket

import pytest

from web2api.network_security import (
    UnsafeOutboundURL,
    install_public_network_guard,
    validate_public_http_url,
    validate_public_websocket_url,
)


def _resolver(address: str):
    def _resolve(host: str, port: int, **kwargs):
        _ = host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return _resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/internal",
        "file:///etc/passwd",
        "https://user:secret@example.com",
        "http://[malformed",
    ],
)
def test_rejects_non_public_outbound_urls(url: str) -> None:
    with pytest.raises(UnsafeOutboundURL):
        validate_public_http_url(url)


def test_rejects_hostname_resolving_to_private_address() -> None:
    with pytest.raises(UnsafeOutboundURL):
        validate_public_http_url(
            "https://internal.example/path",
            resolver=_resolver("10.0.0.5"),
        )


def test_accepts_hostname_when_every_address_is_public() -> None:
    assert validate_public_http_url(
        "https://public.example/path",
        resolver=_resolver("93.184.216.34"),
    ) == "https://public.example/path"


def test_private_network_requires_explicit_escape_hatch() -> None:
    assert validate_public_http_url(
        "http://10.0.0.5/api",
        allow_private_network=True,
    ) == "http://10.0.0.5/api"


def test_websocket_validation_uses_the_same_network_policy() -> None:
    assert validate_public_websocket_url(
        "wss://public.example/socket",
        resolver=_resolver("93.184.216.34"),
    ) == "wss://public.example/socket"
    with pytest.raises(UnsafeOutboundURL):
        validate_public_websocket_url(
            "ws://private.example/socket",
            resolver=_resolver("10.0.0.5"),
        )
    with pytest.raises(UnsafeOutboundURL, match="only ws and wss"):
        validate_public_websocket_url("https://public.example/socket")


class _FakeContext:
    route_handler = None
    websocket_handler = None

    async def route(self, pattern, handler) -> None:  # noqa: ANN001
        assert pattern == "**/*"
        self.route_handler = handler

    async def route_web_socket(self, pattern, handler) -> None:  # noqa: ANN001
        assert pattern == "**/*"
        self.websocket_handler = handler


class _FakeRoute:
    def __init__(self, *, location: str | None = None) -> None:
        self.action: str | None = None
        self.response = _FakeResponse(location=location)

    async def continue_(self) -> None:
        self.action = "continued"

    async def abort(self, reason: str) -> None:
        self.action = reason

    async def fetch(self, *, max_redirects: int):
        assert max_redirects == 0
        return self.response

    async def fulfill(self, *, response) -> None:  # noqa: ANN001
        assert isinstance(response, _FakeResponse)
        self.action = "fulfilled"


class _FakeResponse:
    def __init__(self, *, location: str | None = None) -> None:
        self.headers = {} if location is None else {"location": location}
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeWebSocketRoute:
    def __init__(self, url: str) -> None:
        self.url = url
        self.action: str | None = None

    async def close(self, *, code: int, reason: str) -> None:
        assert code == 1008
        assert reason
        self.action = "closed"

    def connect_to_server(self) -> None:
        self.action = "connected"


@pytest.mark.asyncio
async def test_context_guard_blocks_private_http_and_websocket_requests() -> None:
    context = _FakeContext()
    await install_public_network_guard(context)  # type: ignore[arg-type]
    assert context.route_handler is not None
    assert context.websocket_handler is not None

    public_route = _FakeRoute()
    await context.route_handler(public_route, _FakeRequest("https://example.com/page"))
    assert public_route.action == "fulfilled"
    assert public_route.response.disposed is True

    redirect_route = _FakeRoute(location="http://127.0.0.1/admin")
    await context.route_handler(
        redirect_route,
        _FakeRequest("https://example.com/redirect"),
    )
    assert redirect_route.action == "blockedbyclient"
    assert redirect_route.response.disposed is True

    private_route = _FakeRoute()
    await context.route_handler(private_route, _FakeRequest("http://127.0.0.1/admin"))
    assert private_route.action == "blockedbyclient"

    websocket = _FakeWebSocketRoute("ws://169.254.169.254/metadata")
    await context.websocket_handler(websocket)
    assert websocket.action == "closed"
