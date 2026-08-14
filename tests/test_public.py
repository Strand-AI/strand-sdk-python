"""Public-cohort namespace (`client.public`) request-shape + parsing tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

import strand

DETAIL = {
    "publicId": "pub-1",
    "title": "TCGA slide",
    "thumbnailUrl": "/api/v1/public/samples/pub-1/thumbnail",
    "tags": ["tcga-coad"],
    "metadata": {"stage": "II"},
    "geometry": {"widthPx": 20000, "heightPx": 15000, "mppX": 0.5, "mppY": 0.5},
    "viewer": {
        "pyramidUrl": "/api/v1/public/samples/pub-1/zarr",
        "markers": [{"name": "CD3"}, {"name": "CD8"}],
    },
}


@respx.mock
def test_list_sends_pagination_params_and_parses_page(
    client: strand.Client, api_root: str
) -> None:
    route = respx.get(f"{api_root}/public/samples").mock(
        return_value=Response(
            200,
            json={
                "items": [
                    {
                        "publicId": "pub-1",
                        "title": "TCGA slide",
                        "thumbnailUrl": "/api/v1/public/samples/pub-1/thumbnail",
                        "tags": ["tcga-coad"],
                        "metadata": {"stage": "II"},
                    }
                ],
                "page": 2,
                "pageSize": 10,
                "totalCount": 1,
                "totalPages": 1,
            },
        )
    )

    result = client.public.list(page=2, page_size=10, tag="tcga-coad")

    params = route.calls.last.request.url.params
    assert params["page"] == "2"
    assert params["pageSize"] == "10"
    assert params["tag"] == "tcga-coad"
    assert isinstance(result, strand.PublicSampleList)
    assert result.total_count == 1
    assert result.items[0].public_id == "pub-1"
    assert result.items[0].tags == ["tcga-coad"]


@respx.mock
def test_list_omits_params_when_unset(client: strand.Client, api_root: str) -> None:
    route = respx.get(f"{api_root}/public/samples").mock(
        return_value=Response(
            200,
            json={"items": [], "page": 1, "pageSize": 48, "totalCount": 0, "totalPages": 0},
        )
    )

    client.public.list()

    assert str(route.calls.last.request.url.params) == ""


@respx.mock
def test_get_parses_detail_handle(client: strand.Client, api_root: str) -> None:
    respx.get(f"{api_root}/public/samples/pub-1").mock(return_value=Response(200, json=DETAIL))

    sample = client.public.get("pub-1")

    assert isinstance(sample, strand.PublicSample)
    assert sample.public_id == "pub-1"
    assert sample.markers == ["CD3", "CD8"]
    assert sample.geometry.width_px == 20000
    assert sample.geometry.mpp_x == 0.5
    assert sample.thumbnail_url == "/api/v1/public/samples/pub-1/thumbnail"
    assert sample.pyramid_url == "/api/v1/public/samples/pub-1/zarr"


@respx.mock
def test_get_raises_not_found_for_non_public_sample(
    client: strand.Client, api_root: str
) -> None:
    # An authed caller passing a non-public (or unknown) publicId gets 404 — the
    # is_public gate lives in the server; there is no IDOR to a private sample.
    respx.get(f"{api_root}/public/samples/pub-x").mock(
        return_value=Response(404, json={"error": "not_found", "message": "Public sample not found"})
    )

    with pytest.raises(strand.NotFoundError):
        client.public.get("pub-x")


@respx.mock
def test_download_to_mirrors_the_zarr_store(
    client: strand.Client, api_root: str, tmp_path: Path
) -> None:
    zarr = f"{api_root}/public/samples/pub-1/zarr"
    respx.get(f"{api_root}/public/samples/pub-1").mock(return_value=Response(200, json=DETAIL))
    # Root group: one H&E multiscale with a single level at "he/0".
    respx.get(f"{zarr}/zarr.json").mock(
        return_value=Response(
            200,
            json={
                "zarr_format": 3,
                "node_type": "group",
                "attributes": {
                    "multiscales": [
                        {"name": "H&E", "datasets": [{"path": "he/0"}]},
                    ]
                },
            },
        )
    )
    # One small array: shape == chunk == [3, 2, 2] → a single chunk c/0/0/0.
    array_meta = {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [3, 2, 2],
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [3, 2, 2]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "fill_value": 0,
    }
    respx.get(f"{zarr}/he/0/zarr.json").mock(return_value=Response(200, json=array_meta))
    respx.get(f"{zarr}/he/0/c/0/0/0").mock(return_value=Response(200, content=bytes(range(12))))

    out = client.public.get("pub-1").download_to(tmp_path / "store")

    assert (out / "zarr.json").exists()
    assert json.loads((out / "he/0/zarr.json").read_text())["shape"] == [3, 2, 2]
    assert (out / "he/0/c/0/0/0").read_bytes() == bytes(range(12))
