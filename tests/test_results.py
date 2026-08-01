"""Result-store reads, asserted against the shapes the platform really writes.

The predecessor of this file lived in `test_jobs.py` and built its fixtures from
a hand-written `_array_meta()` that declared `codecs: [bytes]` and one chunk per
object. That contract went stale when marker pyramids moved to zarr v3
`sharding_indexed` + zstd, and because the tests wrote their own metadata they
kept passing while `to_array()` raised on every real result.

So the array metadata here is not written by hand: `PROD_LEVEL_META` is
`markers/PanCK/5/zarr.json` captured verbatim from completed production job
b1e2e282-61bd-4c27-b073-4dc796a91967. `platform/inference/tests/test_pyramid.py`
asserts the writer still emits exactly this codec chain, so a platform-side
codec change fails there and forces this fixture (and the decoder) to be
revisited.

Shard payloads are built by `assemble_shard()` below, a mirror of
`platform/inference/pyramid.py::assemble_shard`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

import strand
from tests.conftest import API_ROOT

np = pytest.importorskip("numpy")
zstd = pytest.importorskip("zstandard")

JOB_ID = "b1e2e282-61bd-4c27-b073-4dc796a91967"
BASE = f"{API_ROOT}/jobs/{JOB_ID}/results"

FIXTURES = Path(__file__).parent / "fixtures"
PROD_LEVEL_META: dict[str, Any] = json.loads(
    (FIXTURES / "prod_marker_level_zarr.json").read_text()
)

_SHARD_EMPTY = (1 << 64) - 1


# ---------- shard writer (mirrors platform/inference/pyramid.py) ----------


def assemble_shard(present: dict[int, bytes], slots: int) -> bytes:
    """`[index: slots x (offset u64 LE, nbytes u64 LE)][zstd chunk frames...]`."""
    index_nbytes = slots * 16
    index: list[tuple[int, int]] = [(_SHARD_EMPTY, _SHARD_EMPTY)] * slots
    body = bytearray()
    compressor = zstd.ZstdCompressor(level=3, write_checksum=False)
    for slot in sorted(present):
        data = compressor.compress(present[slot])
        index[slot] = (index_nbytes + len(body), len(data))
        body.extend(data)
    out = bytearray()
    for offset, nbytes in index:
        out += offset.to_bytes(8, "little")
        out += nbytes.to_bytes(8, "little")
    out += body
    return bytes(out)


def shard_objects(array: Any, meta: dict[str, Any]) -> dict[str, bytes]:
    """Serialize a `[C, H, W]` array into `{"c/0/{sr}/{sc}": shard_bytes}`.

    Inner chunks are zero-padded to the full inner chunk shape, exactly as the
    platform writer does; slots past the array edge stay absent.
    """
    _, h, w = (int(v) for v in meta["shape"])
    shard = [int(v) for v in meta["chunk_grid"]["configuration"]["chunk_shape"]]
    inner = [int(v) for v in meta["codecs"][0]["configuration"]["chunk_shape"]]
    per_y, per_x = shard[1] // inner[1], shard[2] // inner[2]
    slots = per_y * per_x
    chunk_rows, chunk_cols = -(-h // inner[1]), -(-w // inner[2])

    out: dict[str, bytes] = {}
    for sr in range(-(-h // shard[1])):
        for sc in range(-(-w // shard[2])):
            present: dict[int, bytes] = {}
            for iy in range(per_y):
                cr = sr * per_y + iy
                if cr >= chunk_rows:
                    continue
                for ix in range(per_x):
                    cc = sc * per_x + ix
                    if cc >= chunk_cols:
                        continue
                    tile = np.zeros((inner[0], inner[1], inner[2]), dtype=array.dtype)
                    y0, x0 = cr * inner[1], cc * inner[2]
                    region = array[:, y0 : y0 + inner[1], x0 : x0 + inner[2]]
                    tile[:, : region.shape[1], : region.shape[2]] = region
                    present[iy * per_x + ix] = tile.tobytes()
            if present:
                out[f"c/0/{sr}/{sc}"] = assemble_shard(present, slots)
    return out


# ---------- store fixtures ----------


def level_meta(shape: list[int], shard: list[int], inner: list[int]) -> dict[str, Any]:
    """Prod metadata with only the geometry substituted — codec chain untouched."""
    meta = copy.deepcopy(PROD_LEVEL_META)
    meta["shape"] = shape
    meta["chunk_grid"]["configuration"]["chunk_shape"] = shard
    meta["codecs"][0]["configuration"]["chunk_shape"] = inner
    return meta


def root_meta(markers: dict[str, int]) -> dict[str, Any]:
    """Root group as postprocess writes it: `{marker: level_count}`, no H&E entry."""
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {
            "ome": {"version": "0.5"},
            "multiscales": [
                {
                    "version": "0.5",
                    "name": marker,
                    "axes": [
                        {"name": "c", "type": "channel"},
                        {"name": "y", "type": "space", "unit": "pixel"},
                        {"name": "x", "type": "space", "unit": "pixel"},
                    ],
                    "datasets": [
                        {
                            "path": f"markers/{marker}/{i}",
                            "coordinateTransformations": [
                                {"type": "scale", "scale": [1, 2**i, 2**i]}
                            ],
                        }
                        for i in range(levels)
                    ],
                    "type": "gaussian",
                }
                for marker, levels in markers.items()
            ],
        },
    }


def mock_results_endpoint() -> None:
    respx.get(BASE).mock(
        return_value=Response(
            200,
            json={
                "resultUrl": "https://storage.googleapis.com/.../zarr.json?sig=...",
                "resultBasePath": f"results/{JOB_ID}",
                "expiresAt": "2026-08-01T03:11:29Z",
            },
        )
    )


def mock_files(files: dict[str, bytes]) -> None:
    """Serve `files` under `/results/files/`; anything else 404s like the proxy."""
    for path, body in files.items():
        respx.get(f"{BASE}/files/{path}").mock(return_value=Response(200, content=body))
    respx.get(url__startswith=f"{BASE}/files/").mock(
        return_value=Response(
            404, json={"error": "not_found", "message": "Result file not found"}
        )
    )


def job(client: strand.Client) -> strand.Job:
    return strand.Job(id=JOB_ID, reserved_credits=None, client=client)


def ramp(shape: tuple[int, int, int]) -> Any:
    c, h, w = shape
    return (np.arange(c * h * w, dtype=np.float32) % 251.0).reshape(c, h, w)


# ---------- tests ----------


@respx.mock
def test_to_array_decodes_production_sharded_zstd_level(client: strand.Client) -> None:
    """The exact failure users hit: sharding_indexed + zstd raised on every result.

    Uses the captured prod metadata verbatim — a 256x256 level inside a
    2048x2048 shard, i.e. 1 of the shard's 64 index slots occupied and 63
    "not present" sentinels.
    """
    meta = PROD_LEVEL_META
    expected = ramp((1, 256, 256))
    files = {"zarr.json": json.dumps(root_meta({"PanCK": 6})).encode()}
    files["markers/PanCK/0/zarr.json"] = json.dumps(meta).encode()
    for key, blob in shard_objects(expected, meta).items():
        files[f"markers/PanCK/0/{key}"] = blob

    mock_results_endpoint()
    mock_files(files)

    array, got_meta = job(client).results().to_array(name="PanCK")
    assert array.shape == (1, 256, 256)
    assert array.dtype == np.float32
    assert np.array_equal(array, expected)
    assert got_meta["codecs"][0]["name"] == "sharding_indexed"


@respx.mock
def test_to_array_decodes_multi_shard_grid_with_edge_overhang(
    client: strand.Client,
) -> None:
    """Shard-grid and inner-chunk placement, including partial edge shards.

    Prod geometry (256px inner / 2048px shard) is scaled down 8x so the test
    array stays small; the codec chain is still the captured prod one. 300x200
    over 128px shards gives a 3x2 shard grid whose right and bottom shards hang
    off the array edge.
    """
    meta = level_meta([1, 300, 200], [1, 128, 128], [1, 32, 32])
    expected = ramp((1, 300, 200))
    files = {
        "zarr.json": json.dumps(root_meta({"aSMA": 1})).encode(),
        "markers/aSMA/0/zarr.json": json.dumps(meta).encode(),
    }
    shards = shard_objects(expected, meta)
    assert len(shards) == 6, "expected a 3x2 shard grid"
    for key, blob in shards.items():
        files[f"markers/aSMA/0/{key}"] = blob

    mock_results_endpoint()
    mock_files(files)

    array, _ = job(client).results().to_array(name="aSMA")
    assert np.array_equal(array, expected)


@respx.mock
def test_to_array_reads_absent_shard_as_fill_value(client: strand.Client) -> None:
    """A shard with no populated inner chunk is never uploaded — it must read as fill."""
    meta = level_meta([1, 300, 200], [1, 128, 128], [1, 32, 32])
    expected = ramp((1, 300, 200))
    shards = shard_objects(expected, meta)
    dropped = "c/0/1/1"
    del shards[dropped]
    expected = expected.copy()
    expected[:, 128:256, 128:200] = 0.0

    files = {
        "zarr.json": json.dumps(root_meta({"aSMA": 1})).encode(),
        "markers/aSMA/0/zarr.json": json.dumps(meta).encode(),
    }
    for key, blob in shards.items():
        files[f"markers/aSMA/0/{key}"] = blob

    mock_results_endpoint()
    mock_files(files)

    array, _ = job(client).results().to_array(name="aSMA")
    assert np.array_equal(array, expected)


@respx.mock
def test_to_anndata_stacks_sharded_markers(client: strand.Client) -> None:
    pytest.importorskip("anndata")
    meta = level_meta([1, 64, 48], [1, 128, 128], [1, 32, 32])
    panck = ramp((1, 64, 48))
    asma = panck * 2.0

    files = {"zarr.json": json.dumps(root_meta({"PanCK": 1, "aSMA": 1})).encode()}
    for name, array in (("PanCK", panck), ("aSMA", asma)):
        files[f"markers/{name}/0/zarr.json"] = json.dumps(meta).encode()
        for key, blob in shard_objects(array, meta).items():
            files[f"markers/{name}/0/{key}"] = blob

    mock_results_endpoint()
    mock_files(files)

    adata = job(client).results().to_anndata()
    assert adata.shape == (64 * 48, 2)
    assert list(adata.var["channel"]) == ["PanCK", "aSMA"]
    assert np.array_equal(adata.X[:, 0], panck[0].reshape(-1))
    assert np.array_equal(adata.X[:, 1], asma[0].reshape(-1))


@respx.mock
def test_download_to_survives_manifest_declaring_a_level_that_was_never_written(
    client: strand.Client, tmp_path: Path
) -> None:
    """Bug A from the client side.

    Results written before the platform-side fix declare 7 datasets per marker
    while the writer emitted 6, so `markers/<name>/6` 404s. The mirror must
    still complete, and the local `zarr.json` must describe only what it holds
    so the copy is an openable zarr store.
    """
    meta = level_meta([1, 64, 48], [1, 128, 128], [1, 32, 32])
    array = ramp((1, 64, 48))

    # Manifest claims 3 levels; storage holds 2.
    files = {"zarr.json": json.dumps(root_meta({"PanCK": 3})).encode()}
    for level in (0, 1):
        files[f"markers/PanCK/{level}/zarr.json"] = json.dumps(meta).encode()
        for key, blob in shard_objects(array, meta).items():
            files[f"markers/PanCK/{level}/{key}"] = blob

    mock_results_endpoint()
    mock_files(files)

    target = tmp_path / "out"
    with pytest.warns(UserWarning, match="markers/PanCK/2"):
        out = job(client).results().download_to(target)

    assert out == target
    assert (target / "markers" / "PanCK" / "0" / "zarr.json").exists()
    assert (target / "markers" / "PanCK" / "1" / "zarr.json").exists()
    assert not (target / "markers" / "PanCK" / "2").exists()

    mirrored = json.loads((target / "zarr.json").read_text())
    paths = [d["path"] for d in mirrored["attributes"]["multiscales"][0]["datasets"]]
    assert paths == ["markers/PanCK/0", "markers/PanCK/1"]


@respx.mock
def test_unsupported_codec_names_the_chain(client: strand.Client) -> None:
    meta = copy.deepcopy(PROD_LEVEL_META)
    meta["codecs"][0]["configuration"]["codecs"][1] = {
        "name": "blosc",
        "configuration": {"cname": "zstd"},
    }
    mock_results_endpoint()
    mock_files(
        {
            "zarr.json": json.dumps(root_meta({"PanCK": 1})).encode(),
            "markers/PanCK/0/zarr.json": json.dumps(meta).encode(),
        }
    )

    with pytest.raises(strand.StrandError, match="sharding payload"):
        job(client).results().to_array(name="PanCK")
