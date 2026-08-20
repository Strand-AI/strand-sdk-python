"""Public branch of unified samples.get and public byte-handle tests."""

from __future__ import annotations

import json
from pathlib import Path

import respx
from httpx import Response

import strand

DETAIL = {
    "ownership": "public",
    "id": "pub-1",
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
def test_samples_get_parses_public_detail_handle(
    client: strand.Client, api_root: str
) -> None:
    route = respx.get(f"{api_root}/samples/pub-1").mock(
        return_value=Response(200, json=DETAIL)
    )

    sample = client.samples.get("pub-1")

    assert route.calls.last.request.method == "GET"
    assert isinstance(sample, strand.PublicSample)
    assert sample.ownership == "public"
    assert sample.id == "pub-1"
    assert not hasattr(sample, "public_id")
    assert sample.markers == ["CD3", "CD8"]
    assert sample.geometry.width_px == 20000
    assert sample.geometry.mpp_x == 0.5
    assert sample.thumbnail_url == "/api/v1/public/samples/pub-1/thumbnail"
    assert sample.pyramid_url == "/api/v1/public/samples/pub-1/zarr"


@respx.mock
def test_public_to_dict_is_exact_serializable_public_detail(
    client: strand.Client, api_root: str
) -> None:
    respx.get(f"{api_root}/samples/pub-1").mock(return_value=Response(200, json=DETAIL))

    sample = client.samples.get("pub-1")
    assert isinstance(sample, strand.PublicSample)
    detail = sample.to_dict()

    assert set(detail) == {
        "ownership",
        "id",
        "title",
        "tags",
        "metadata",
        "geometry",
        "markers",
        "thumbnail_url",
        "pyramid_url",
    }
    assert detail == {
        "ownership": "public",
        "id": "pub-1",
        "title": "TCGA slide",
        "tags": ["tcga-coad"],
        "metadata": {"stage": "II"},
        "geometry": {
            "width_px": 20000,
            "height_px": 15000,
            "mpp_x": 0.5,
            "mpp_y": 0.5,
        },
        "markers": ["CD3", "CD8"],
        "thumbnail_url": "/api/v1/public/samples/pub-1/thumbnail",
        "pyramid_url": "/api/v1/public/samples/pub-1/zarr",
    }
    assert json.loads(json.dumps(detail)) == detail
    forbidden = {
        "_http",
        "_root_cache",
        "public_id",
        "internal_sample_id",
        "org_id",
        "storage_path",
        "jobs",
        "job_count",
    }
    assert set(detail).isdisjoint(forbidden)


@respx.mock
def test_download_to_mirrors_the_retained_public_zarr_route(
    client: strand.Client, api_root: str, tmp_path: Path
) -> None:
    zarr = f"{api_root}/public/samples/pub-1/zarr"
    respx.get(f"{api_root}/samples/pub-1").mock(return_value=Response(200, json=DETAIL))
    respx.get(f"{zarr}/zarr.json").mock(
        return_value=Response(
            200,
            json={
                "zarr_format": 3,
                "node_type": "group",
                "attributes": {"multiscales": [{"name": "H&E", "datasets": [{"path": "he/0"}]}]},
            },
        )
    )
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
    respx.get(f"{zarr}/he/0/c/0/0/0").mock(
        return_value=Response(200, content=bytes(range(12)))
    )

    sample = client.samples.get("pub-1")
    assert isinstance(sample, strand.PublicSample)
    out = sample.download_to(tmp_path / "store")

    assert (out / "zarr.json").exists()
    assert json.loads((out / "he/0/zarr.json").read_text())["shape"] == [3, 2, 2]
    assert (out / "he/0/c/0/0/0").read_bytes() == bytes(range(12))
