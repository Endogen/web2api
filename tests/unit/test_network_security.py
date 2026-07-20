"""Tests for outbound URL validation."""

from __future__ import annotations

import socket

import pytest

from web2api.network_security import UnsafeOutboundURL, validate_public_http_url


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
