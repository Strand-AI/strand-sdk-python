from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

import strand

API_KEY = "sk-strand-test-0000000000000000"
BASE_URL = "https://test.strandai.example"
API_ROOT = f"{BASE_URL}/api/v1"


@pytest.fixture
def client() -> Iterator[strand.Client]:
    c = strand.Client(api_key=API_KEY, base_url=BASE_URL, timeout=5.0)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def api_root() -> str:
    return API_ROOT


@pytest.fixture
def auth_header() -> dict[str, str]:
    return {"authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def httpx_transport_assert():
    """Sanity-check that the client really sent Authorization."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    return seen, _record
