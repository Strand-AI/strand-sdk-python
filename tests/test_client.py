"""High-level surface checks: auth header, env-var fallback, error mapping."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

import strand
from tests.conftest import API_KEY, API_ROOT, BASE_URL


def test_client_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRAND_API_KEY", API_KEY)
    monkeypatch.setenv("STRAND_BASE_URL", BASE_URL)
    c = strand.Client()
    assert c.api_root == API_ROOT
    c.close()


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRAND_API_KEY", raising=False)
    with pytest.raises(strand.AuthError):
        strand.Client(base_url=BASE_URL)


@respx.mock
def test_estimate_happy_path(client: strand.Client, api_root: str) -> None:
    respx.post(f"{api_root}/predict/estimate").mock(
        return_value=Response(
            200,
            json={
                "patchCount": 100,
                "markerCount": 3,
                "estimatedCredits": 300,
                "orgBalance": 1000,
                "orgPending": 50,
            },
        )
    )

    estimate = client.predict.estimate("upload-uuid", markers=["CD3", "CD8", "Ki67"])

    assert estimate.estimated_credits == 300
    assert estimate.patch_count == 100
    assert estimate.org_balance == 1000


@respx.mock
def test_estimate_sends_bearer_header(client: strand.Client, api_root: str) -> None:
    route = respx.post(f"{api_root}/predict/estimate").mock(
        return_value=Response(
            200,
            json={
                "patchCount": 1,
                "markerCount": 1,
                "estimatedCredits": 1,
                "orgBalance": 1,
                "orgPending": 0,
            },
        )
    )
    client.predict.estimate("u", markers=["CD3"])
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["user-agent"].startswith("strand-sdk-python/")


@respx.mock
def test_402_insufficient_credits_maps_to_typed_exception(
    client: strand.Client, api_root: str
) -> None:
    respx.post(f"{api_root}/predict").mock(
        return_value=Response(
            402,
            json={
                "error": "insufficient_credits",
                "message": "Need 1000 credits",
                "required": 1000,
            },
        )
    )

    with pytest.raises(strand.InsufficientCreditsError) as exc_info:
        client.predict.submit("u", markers=["CD3"])

    err = exc_info.value
    assert err.required == 1000
    assert err.message == "Need 1000 credits"
    assert err.status_code == 402


@respx.mock
def test_429_rate_limit_carries_retry_after(client: strand.Client, api_root: str) -> None:
    respx.post(f"{api_root}/predict").mock(
        return_value=Response(
            429,
            json={"error": "rate_limited", "message": "Concurrent cap exceeded"},
            headers={"Retry-After": "30"},
        )
    )

    with pytest.raises(strand.RateLimitError) as exc_info:
        client.predict.submit("u", markers=["CD3"])

    assert exc_info.value.retry_after == 30


@respx.mock
def test_401_maps_to_auth_error(client: strand.Client, api_root: str) -> None:
    respx.post(f"{api_root}/predict/estimate").mock(
        return_value=Response(401, json={"error": "unauthorized", "message": "Invalid key"})
    )

    with pytest.raises(strand.AuthError) as exc_info:
        client.predict.estimate("u", markers=["CD3"])
    assert exc_info.value.status_code == 401


@respx.mock
def test_400_maps_to_bad_request(client: strand.Client, api_root: str) -> None:
    respx.post(f"{api_root}/predict/estimate").mock(
        return_value=Response(400, json={"error": "bad_request", "message": "Invalid markers"})
    )

    with pytest.raises(strand.BadRequestError):
        client.predict.estimate("u", markers=["CD3"])


def test_markers_validation(client: strand.Client) -> None:
    with pytest.raises(ValueError, match="at least one"):
        client.predict.estimate("u", markers=[])
    with pytest.raises(ValueError, match="at least one"):
        client.predict.estimate("u", markers=["  ", ""])


def test_version_matches_distribution_metadata() -> None:
    """`strand.__version__` must come from package metadata so it can't drift
    from pyproject.toml. Regression for #98 where the literal `"0.3.0"` lagged
    behind the published `0.4.0`."""
    from importlib.metadata import version as _v

    assert strand.__version__ == _v("strand-sdk")
    assert strand.__version__ != "unknown"
