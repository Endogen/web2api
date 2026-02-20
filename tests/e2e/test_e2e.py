"""End-to-end tests that exercise the Dockerized Web2API service."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from web2api.schemas import ApiResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
BASE_URL = "http://127.0.0.1:8010"
HEALTH_TIMEOUT_SECONDS = 180
HEALTH_POLL_INTERVAL_SECONDS = 2.0
LIVE_ERROR_MARKERS = (
    "err_name_not_resolved",
    "could not resolve",
    "name or service not known",
    "dns",
    "connection refused",
    "connection reset",
    "sandbox_host_linux.cc",
)
DOCKER_UNAVAILABLE_MARKERS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "permission denied",
    "docker daemon",
    "cannot connect to the docker engine",
)


def _docker_compose_base_cmd() -> list[str]:
    docker_binary = shutil.which("docker")
    if docker_binary is None:
        pytest.skip("Docker executable was not found in PATH.")

    version_result = subprocess.run(
        [docker_binary, "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if version_result.returncode != 0:
        combined = f"{version_result.stdout}\n{version_result.stderr}".strip()
        pytest.skip(f"docker compose is unavailable: {combined}")

    return [docker_binary, "compose", "-f", str(COMPOSE_FILE)]


def _is_docker_unavailable(stderr_stdout: str) -> bool:
    message = stderr_stdout.lower()
    return any(marker in message for marker in DOCKER_UNAVAILABLE_MARKERS)


def _wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_error = "no response"

    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/health")
                if response.status_code == 200:
                    payload = response.json()
                    if payload.get("status") in {"ok", "healthy"}:
                        return
                    last_error = f"unexpected health payload: {payload}"
                else:
                    last_error = f"health status {response.status_code}: {response.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)

    pytest.fail(f"Timed out waiting for /health after {HEALTH_TIMEOUT_SECONDS}s ({last_error}).")


def _run_compose(
    base_cmd: list[str],
    args: list[str],
    *,
    env: dict[str, str],
    check: bool,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [*base_cmd, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}".strip()
        if _is_docker_unavailable(combined):
            pytest.skip(f"Docker environment unavailable: {combined}")
        pytest.fail(f"docker compose {' '.join(args)} failed:\n{combined}")
    return result


def _assert_live_result_or_skip(response: ApiResponse, *, endpoint: str) -> None:
    if response.error is None:
        return

    message = response.error.message.lower()
    if any(marker in message for marker in LIVE_ERROR_MARKERS):
        pytest.skip(f"Live network unavailable for {endpoint}: {response.error.message}")

    pytest.fail(f"{endpoint} failed with {response.error.code}: {response.error.message}")


def _should_skip_for_network_issue(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in LIVE_ERROR_MARKERS)


@pytest.fixture
def dockerized_web2api() -> Iterator[str]:
    compose_cmd = _docker_compose_base_cmd()
    compose_project = f"web2api-e2e-{uuid.uuid4().hex[:8]}"
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = compose_project

    started = False
    try:
        up_result = _run_compose(compose_cmd, ["up", "--build", "-d"], env=env, check=False)
        if up_result.returncode != 0:
            combined = f"{up_result.stdout}\n{up_result.stderr}".strip()
            if _is_docker_unavailable(combined):
                pytest.skip(f"Docker environment unavailable: {combined}")
            pytest.fail(f"docker compose up failed:\n{combined}")

        started = True
        _wait_for_health(BASE_URL)
        yield BASE_URL
    finally:
        if started:
            _run_compose(
                compose_cmd,
                ["down", "--volumes", "--remove-orphans"],
                env=env,
                check=False,
            )


def test_docker_e2e_hackernews_flow(dockerized_web2api: str) -> None:
    with httpx.Client(base_url=dockerized_web2api, timeout=30.0) as client:
        catalog_response = client.get("/api/recipes/manage")
        if catalog_response.status_code != 200:
            if _should_skip_for_network_issue(catalog_response.text):
                pytest.skip(f"Catalog unavailable due to network issues: {catalog_response.text}")
            assert catalog_response.status_code == 200, catalog_response.text
        catalog_payload = catalog_response.json()
        catalog_error = str(catalog_payload.get("catalog_error") or "")
        if catalog_error and _should_skip_for_network_issue(catalog_error):
            pytest.skip(f"Catalog unavailable due to network issues: {catalog_error}")
        catalog_entries = catalog_payload.get("catalog", [])
        assert isinstance(catalog_entries, list)

        hackernews_entry = next(
            (entry for entry in catalog_entries if entry.get("name") == "hackernews"),
            None,
        )
        assert hackernews_entry is not None, "hackernews entry missing from catalog"

        if not hackernews_entry.get("installed"):
            install_response = client.post("/api/recipes/manage/install/hackernews")
            assert install_response.status_code == 200, install_response.text

        read_raw = client.get("/hackernews/read")
        assert read_raw.status_code == 200, read_raw.text
        read_response = ApiResponse.model_validate(read_raw.json())
        _assert_live_result_or_skip(read_response, endpoint="read")
        assert read_response.error is None
        assert read_response.items
        assert read_response.metadata.item_count > 0
        assert read_response.pagination.current_page == 1
        assert read_response.pagination.has_next is True
        assert read_response.items[0].title is not None
        assert read_response.items[0].title.strip()
        assert read_response.items[0].url is not None
        assert read_response.items[0].url.startswith(("http://", "https://"))

        read_page_two_raw = client.get("/hackernews/read", params={"page": 2})
        assert read_page_two_raw.status_code == 200, read_page_two_raw.text
        read_page_two = ApiResponse.model_validate(read_page_two_raw.json())
        _assert_live_result_or_skip(read_page_two, endpoint="read page 2")
        assert read_page_two.error is None
        assert read_page_two.pagination.current_page == 2
        assert read_page_two.pagination.has_prev is True

        search_raw = client.get("/hackernews/search", params={"q": "python"})
        assert search_raw.status_code == 200, search_raw.text
        search_response = ApiResponse.model_validate(search_raw.json())
        _assert_live_result_or_skip(search_response, endpoint="search")
        assert search_response.error is None
        assert search_response.endpoint == "search"
        assert search_response.query == "python"
        assert search_response.items

        sites_response = client.get("/api/sites")
        assert sites_response.status_code == 200, sites_response.text
        sites_payload = sites_response.json()
        assert isinstance(sites_payload, list)
        assert any(site.get("slug") == "hackernews" for site in sites_payload)
