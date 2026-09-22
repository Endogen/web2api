"""Real-browser regression coverage for context-wide outbound filtering."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from web2api.network_security import UnsafeOutboundURL, install_public_network_guard

pytestmark = pytest.mark.e2e


class _ProbeServer(ThreadingHTTPServer):
    internal_hits: list[str]
    internal_port: int
    is_internal: bool


class _ProbeHandler(BaseHTTPRequestHandler):
    server: _ProbeServer

    def do_GET(self) -> None:  # noqa: N802
        if self.server.is_internal:
            self.server.internal_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            return

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{self.server.internal_port}/redirect-target",
            )
            self.end_headers()
            return
        if self.path == "/sw.js":
            body = (
                "self.addEventListener('install', () => "
                f"fetch('http://127.0.0.1:{self.server.internal_port}/service-worker'));"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        internal_url = f"http://127.0.0.1:{self.server.internal_port}"
        body = f"""
            <!doctype html>
            <iframe src="{internal_url}/frame"></iframe>
            <script>
              fetch('{internal_url}/fetch').catch(() => {{}});
              fetch('/redirect').catch(() => {{}});
              window.open('{internal_url}/popup', '_blank');
              try {{ new WebSocket('ws://127.0.0.1:{self.server.internal_port}/socket'); }}
              catch (error) {{}}
              navigator.serviceWorker.register('/sw.js').catch(() => {{}});
            </script>
        """.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _probe_server(*, is_internal: bool, internal_port: int = 0) -> Iterator[_ProbeServer]:
    server = _ProbeServer(("127.0.0.1", 0), _ProbeHandler)
    server.internal_hits = []
    server.internal_port = internal_port
    server.is_internal = is_internal
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_browser_guard_covers_secondary_request_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _probe_server(is_internal=True) as internal_server:
        with _probe_server(
            is_internal=False,
            internal_port=internal_server.server_address[1],
        ) as public_server:
            public_port = public_server.server_address[1]

            def validate_http(url: str, **kwargs: object) -> str:
                del kwargs
                if urlsplit(url).port != public_port:
                    raise UnsafeOutboundURL("blocked test destination")
                return url

            def validate_websocket(url: str, **kwargs: object) -> str:
                del kwargs
                if urlsplit(url).port != public_port:
                    raise UnsafeOutboundURL("blocked test destination")
                return url

            monkeypatch.setattr(
                "web2api.network_security.validate_public_http_url",
                validate_http,
            )
            monkeypatch.setattr(
                "web2api.network_security.validate_public_websocket_url",
                validate_websocket,
            )

            await _exercise_browser_guard(public_port)

    assert internal_server.internal_hits == []


async def _exercise_browser_guard(public_port: int) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
        context = await browser.new_context(service_workers="block")
        try:
            await install_public_network_guard(context)
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{public_port}/")
            await page.wait_for_timeout(750)
        finally:
            await context.close()
            await browser.close()
