"""Samples namespace request-shape and validation tests."""

from __future__ import annotations

import math

import pytest
import respx
from httpx import Response

import strand


@respx.mock
def test_set_mpp_sends_isotropic_scalar(client: strand.Client, api_root: str) -> None:
    route = respx.patch(f"{api_root}/samples/sample-1/mpp").mock(
        return_value=Response(
            200,
            json={"id": "sample-1", "userMpp": {"x": 0.26, "y": 0.26}},
        )
    )

    result = client.samples.set_mpp("sample-1", 0.26)

    assert route.calls.last.request.content == b'{"mpp":0.26}'
    assert result == {"id": "sample-1", "userMpp": {"x": 0.26, "y": 0.26}}


@respx.mock
def test_set_mpp_sends_both_axes(client: strand.Client, api_root: str) -> None:
    route = respx.patch(f"{api_root}/samples/sample-1/mpp").mock(
        return_value=Response(
            200,
            json={"id": "sample-1", "userMpp": {"x": 0.26, "y": 0.25}},
        )
    )

    client.samples.set_mpp("sample-1", 0.26, 0.25)

    assert route.calls.last.request.content == b'{"mpp":{"x":0.26,"y":0.25}}'


@pytest.mark.parametrize("value", [0, -0.1, 100.1, math.inf, math.nan, True, "0.5"])
def test_set_mpp_rejects_invalid_values(client: strand.Client, value: object) -> None:
    with pytest.raises(ValueError, match="greater than 0 and at most 100"):
        client.samples.set_mpp("sample-1", value)  # type: ignore[arg-type]


@respx.mock
def test_list_tags_unwraps_the_envelope(client: strand.Client, api_root: str) -> None:
    respx.get(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(
            200,
            json={"tags": [{"tag": "histowiz", "createdAt": "2026-07-31T10:00:00Z"}]},
        )
    )

    assert client.samples.list_tags("sample-1") == [
        {"tag": "histowiz", "createdAt": "2026-07-31T10:00:00Z"}
    ]


@respx.mock
def test_list_tags_returns_empty_list_when_untagged(
    client: strand.Client, api_root: str
) -> None:
    respx.get(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(200, json={"tags": []})
    )

    assert client.samples.list_tags("sample-1") == []


@respx.mock
def test_add_tag_posts_the_tag(client: strand.Client, api_root: str) -> None:
    route = respx.post(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(
            200,
            json={"tag": "histowiz", "createdAt": "2026-07-31T10:00:00Z", "created": True},
        )
    )

    result = client.samples.add_tag("sample-1", "HistoWiz")

    assert route.calls.last.request.content == b'{"tag":"HistoWiz"}'
    assert result["created"] is True


@respx.mock
def test_add_tag_is_idempotent(client: strand.Client, api_root: str) -> None:
    # Re-tagging is a 200 with created=False, not an error — a pipeline re-run
    # must not have to special-case it.
    respx.post(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(
            200,
            json={"tag": "histowiz", "createdAt": "2026-07-01T00:00:00Z", "created": False},
        )
    )

    assert client.samples.add_tag("sample-1", "histowiz")["created"] is False


@respx.mock
def test_add_tag_raises_not_found_outside_org(
    client: strand.Client, api_root: str
) -> None:
    respx.post(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(404, json={"error": "not_found", "message": "Sample not found"})
    )

    with pytest.raises(strand.NotFoundError):
        client.samples.add_tag("sample-1", "histowiz")


@respx.mock
def test_remove_tag_sends_tag_as_query_param(
    client: strand.Client, api_root: str
) -> None:
    route = respx.delete(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(200, json={"removed": True})
    )

    assert client.samples.remove_tag("sample-1", "histowiz") is True
    assert route.calls.last.request.url.params["tag"] == "histowiz"


@respx.mock
def test_remove_tag_returns_false_when_absent(
    client: strand.Client, api_root: str
) -> None:
    respx.delete(f"{api_root}/samples/sample-1/tags").mock(
        return_value=Response(200, json={"removed": False})
    )

    assert client.samples.remove_tag("sample-1", "nope") is False
