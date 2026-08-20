"""Scoped sample listing, unified detail, and patch contract tests."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from httpx import Response

import strand


def _owned_detail(**overrides: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "ownership": "mine",
        "id": "sample-1",
        "name": "Slide A",
        "filename": "slide-a.svs",
        "status": "ready",
        "fileSize": "1048576",
        "widthPx": 20000,
        "heightPx": 15000,
        "mpp": 0.5,
        "tags": ["cohort-a", "histowiz"],
        "createdAt": "2026-01-15T12:00:00Z",
        "expiresAt": "2026-12-31T00:00:00Z",
        "expiresAtSource": "custom",
        "expiresInDays": 120,
        "willExpire": True,
        "trashedAt": None,
        "jobs": [],
        "jobCount": 0,
    }
    detail.update(overrides)
    return detail


@respx.mock
def test_list_default_sends_mine_and_default_limit(
    client: strand.Client, api_root: str
) -> None:
    route = respx.get(f"{api_root}/samples").mock(
        return_value=Response(200, json={"items": [], "nextCursor": None})
    )

    result = client.samples.list()

    assert dict(route.calls.last.request.url.params) == {"scope": "mine", "limit": "48"}
    assert result == strand.SampleList(items=[], next_cursor=None)


@pytest.mark.parametrize("scope", ["mine", "public", "all"])
@respx.mock
def test_list_maps_each_scope_and_all_query_fields(
    client: strand.Client, api_root: str, scope: strand.SampleScope
) -> None:
    route = respx.get(f"{api_root}/samples").mock(
        return_value=Response(200, json={"items": [], "nextCursor": "next-2"})
    )

    result = client.samples.list(scope=scope, limit=17, cursor="cursor-1", tag="trial-42")

    assert dict(route.calls.last.request.url.params) == {
        "scope": scope,
        "limit": "17",
        "cursor": "cursor-1",
        "tag": "trial-42",
    }
    assert result.next_cursor == "next-2"


@pytest.mark.parametrize("scope", [None, "", "Mine", "PUBLIC", "m", 1, True])
@respx.mock
def test_list_rejects_invalid_scope_before_request(
    client: strand.Client, scope: object
) -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        client.samples.list(scope=scope)  # type: ignore[arg-type]
    assert len(respx.calls) == 0


@respx.mock
def test_list_parses_discriminated_summary_models(
    client: strand.Client, api_root: str
) -> None:
    respx.get(f"{api_root}/samples").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "ownership": "mine",
                        "id": "mine-1",
                        "name": None,
                        "filename": "mine.svs",
                        "status": "ready",
                        "fileSize": "9223372036854775807",
                        "tags": ["owned"],
                        "createdAt": "2026-08-19T12:30:00Z",
                    },
                    {
                        "ownership": "public",
                        "id": "public-1",
                        "title": "TCGA slide",
                        "thumbnailUrl": "/api/v1/public/samples/public-1/thumbnail",
                        "tags": ["tcga"],
                        "metadata": {"stage": "II"},
                    },
                ],
                "nextCursor": "cursor-2",
            },
        )
    )

    result = client.samples.list(scope="all")

    mine, public = result.items
    assert isinstance(mine, strand.MineSampleSummary)
    assert mine.ownership == "mine"
    assert mine.file_size == 9223372036854775807
    assert mine.created_at == datetime(2026, 8, 19, 12, 30, tzinfo=UTC)
    assert isinstance(public, strand.PublicSampleSummary)
    assert public.ownership == "public"
    assert public.id == "public-1"
    assert public.metadata == {"stage": "II"}
    assert not hasattr(public, "public_id")
    assert result.next_cursor == "cursor-2"


@respx.mock
def test_get_parses_owned_sample_with_empty_job_history(
    client: strand.Client, api_root: str
) -> None:
    route = respx.get(f"{api_root}/samples/sample-1").mock(
        return_value=Response(200, json=_owned_detail())
    )

    result = client.samples.get("sample-1")

    assert route.calls.last.request.method == "GET"
    assert isinstance(result, strand.Sample)
    assert result.ownership == "mine"
    assert result.file_size == 1048576
    assert result.created_at == datetime(2026, 1, 15, 12, tzinfo=UTC)
    assert result.jobs == []
    assert result.job_count == 0


@respx.mock
def test_get_parses_owned_sample_job_history_including_partial_failed(
    client: strand.Client, api_root: str
) -> None:
    respx.get(f"{api_root}/samples/sample-1").mock(
        return_value=Response(
            200,
            json=_owned_detail(
                jobs=[
                    {
                        "id": "job-2",
                        "status": "partial_failed",
                        "progress": 1,
                        "reservedCredits": 12,
                        "markers": ["CD3", "CD8"],
                        "createdAt": "2026-08-19T12:00:00Z",
                        "startedAt": "2026-08-19T12:01:00Z",
                        "completedAt": "2026-08-19T12:02:00Z",
                        "errorMessage": "CD8 failed",
                        "resultsAvailable": True,
                    },
                    {
                        "id": "job-1",
                        "status": "queued",
                        "progress": None,
                        "reservedCredits": None,
                        "markers": ["CD3"],
                        "createdAt": None,
                        "startedAt": None,
                        "completedAt": None,
                        "errorMessage": None,
                        "resultsAvailable": False,
                    },
                ],
                jobCount=57,
            ),
        )
    )

    result = client.samples.get("sample-1")

    assert isinstance(result, strand.Sample)
    assert result.job_count == 57
    assert [job.status for job in result.jobs] == ["partial_failed", "queued"]
    assert result.jobs[0].completed_at == datetime(2026, 8, 19, 12, 2, tzinfo=UTC)
    assert result.jobs[0].is_terminal is True
    assert not hasattr(result.jobs[0], "model")


@respx.mock
def test_get_raises_not_found(client: strand.Client, api_root: str) -> None:
    respx.get(f"{api_root}/samples/missing").mock(
        return_value=Response(404, json={"error": "not_found", "message": "Sample not found"})
    )
    with pytest.raises(strand.NotFoundError):
        client.samples.get("missing")


@pytest.mark.parametrize(
    ("kwargs", "expected_body"),
    [
        ({"name": None}, b'{"name":null}'),
        ({"tags": []}, b'{"tags":[]}'),
        ({"tags": ["trial", "site-a"]}, b'{"tags":["trial","site-a"]}'),
        ({"mpp": 0.26}, b'{"mpp":0.26}'),
        (
            {"name": "Slide B", "tags": ["trial"], "mpp": 0.5},
            b'{"name":"Slide B","tags":["trial"],"mpp":0.5}',
        ),
    ],
)
@respx.mock
def test_patch_serializes_only_supplied_fields(
    client: strand.Client,
    api_root: str,
    kwargs: dict[str, Any],
    expected_body: bytes,
) -> None:
    route = respx.patch(f"{api_root}/samples/sample-1").mock(
        return_value=Response(200, json=_owned_detail(**kwargs))
    )

    result = client.samples.patch("sample-1", **kwargs)

    assert route.calls.last.request.content == expected_body
    assert result.ownership == "mine"


@respx.mock
def test_patch_explicit_unset_omits_field(client: strand.Client, api_root: str) -> None:
    route = respx.patch(f"{api_root}/samples/sample-1").mock(
        return_value=Response(200, json=_owned_detail(mpp=0.3))
    )

    client.samples.patch("sample-1", name=strand.UNSET, mpp=0.3)

    assert route.calls.last.request.content == b'{"mpp":0.3}'


@respx.mock
def test_patch_rejects_all_unset_before_request(client: strand.Client) -> None:
    with pytest.raises(ValueError, match="at least one"):
        client.samples.patch("sample-1")
    assert len(respx.calls) == 0


@respx.mock
def test_patch_rejects_string_as_tag_sequence_before_request(client: strand.Client) -> None:
    with pytest.raises(ValueError, match="not a string"):
        client.samples.patch("sample-1", tags="trial")
    assert len(respx.calls) == 0


@pytest.mark.parametrize("value", [0, -0.1, 100.1, math.inf, math.nan, True, "0.5"])
@respx.mock
def test_patch_rejects_invalid_mpp_before_request(
    client: strand.Client, value: object
) -> None:
    with pytest.raises(ValueError, match="greater than 0 and at most 100"):
        client.samples.patch("sample-1", mpp=value)  # type: ignore[arg-type]
    assert len(respx.calls) == 0
