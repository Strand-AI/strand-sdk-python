"""High-level auth, metadata, surface-removal, and error-mapping checks."""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from importlib import reload
from importlib.metadata import version

import pytest
import respx
from httpx import Response

import strand
from strand._http import USER_AGENT
from tests.conftest import API_KEY, API_ROOT, BASE_URL

_ESTIMATE = {
    "dryRun": True,
    "patchCount": 100,
    "markerCount": 3,
    "estimatedCredits": 300,
    "orgBalance": 1000,
    "orgPending": 50,
}


def test_client_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRAND_API_KEY", API_KEY)
    monkeypatch.setenv("STRAND_BASE_URL", BASE_URL)
    client = strand.Client()
    assert client.api_root == API_ROOT
    client.close()


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRAND_API_KEY", raising=False)
    with pytest.raises(strand.AuthError):
        strand.Client(base_url=BASE_URL)


@respx.mock
def test_oauth_access_token_takes_precedence_over_api_key() -> None:
    route = respx.post(f"{API_ROOT}/predict").mock(return_value=Response(200, json=_ESTIMATE))
    client = strand.Client(
        access_token="oauth-access-token",
        api_key=API_KEY,
        base_url=BASE_URL,
    )
    try:
        client.predict.submit("u", markers=["CD3"], dry_run=True)
    finally:
        client.close()

    assert route.calls.last.request.headers["authorization"] == "Bearer oauth-access-token"


@respx.mock
def test_dry_run_happy_path(client: strand.Client, api_root: str) -> None:
    respx.post(f"{api_root}/predict").mock(return_value=Response(200, json=_ESTIMATE))

    estimate = client.predict.submit(
        "upload-uuid", markers=["CD3", "CD8", "Ki67"], dry_run=True
    )

    assert isinstance(estimate, strand.Estimate)
    assert estimate.estimated_credits == 300
    assert estimate.patch_count == 100
    assert estimate.org_balance == 1000


@respx.mock
def test_user_agent_uses_installed_distribution_version(
    client: strand.Client, api_root: str
) -> None:
    route = respx.post(f"{api_root}/predict").mock(return_value=Response(200, json=_ESTIMATE))

    client.predict.submit("u", markers=["CD3"], dry_run=True)

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert f"strand-sdk-python/{version('strand-sdk')}" == USER_AGENT
    assert request.headers["user-agent"] == USER_AGENT
    assert USER_AGENT != "strand-sdk-python/0.2.0"


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


@pytest.mark.parametrize(
    ("status", "error", "exception"),
    [
        (401, "unauthorized", strand.AuthError),
        (400, "bad_request", strand.BadRequestError),
    ],
)
@respx.mock
def test_dry_run_maps_http_errors(
    client: strand.Client,
    api_root: str,
    status: int,
    error: str,
    exception: type[Exception],
) -> None:
    respx.post(f"{api_root}/predict").mock(
        return_value=Response(status, json={"error": error, "message": "Request failed"})
    )
    with pytest.raises(exception):
        client.predict.submit("u", markers=["CD3"], dry_run=True)


def test_markers_validation(client: strand.Client) -> None:
    with pytest.raises(ValueError, match="at least one"):
        client.predict.submit("u", markers=[], dry_run=True)
    with pytest.raises(ValueError, match="at least one"):
        client.predict.submit("u", markers=["  ", ""], dry_run=True)


def test_superseded_namespaces_methods_and_exports_are_absent(client: strand.Client) -> None:
    assert not hasattr(client, "public")
    assert not hasattr(client.predict, "estimate")
    for method in ("set_mpp", "list_tags", "add_tag", "remove_tag"):
        assert not hasattr(client.samples, method)
    for removed_export in ("PublicSamples", "PublicSampleList"):
        assert removed_export not in strand.__all__
        assert not hasattr(strand, removed_export)


def test_user_agent_is_derived_at_import_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from strand import _http

    with monkeypatch.context() as patch:
        patch.setattr(
            importlib_metadata,
            "version",
            lambda name: "9.8.7" if name == "strand-sdk" else "unexpected",
        )
        reload(_http)
        assert _http.USER_AGENT == "strand-sdk-python/9.8.7"

    reload(_http)
    assert f"strand-sdk-python/{version('strand-sdk')}" == _http.USER_AGENT


def test_version_matches_distribution_metadata() -> None:
    assert strand.__version__ == version("strand-sdk")
    assert strand.__version__ != "unknown"
