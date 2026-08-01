"""Job tests: submit, refresh, SSE wait, results download."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
import respx
from httpx import Response

import strand
from tests.conftest import API_ROOT


@respx.mock
def test_submit_returns_job_with_reserved_credits(client: strand.Client) -> None:
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={
                "jobId": "22222222-2222-2222-2222-222222222222",
                "reservedCredits": 300,
                "status": "queued",
            },
        )
    )
    job = client.predict.submit("upload-id", markers=["CD3"])
    assert job.id == "22222222-2222-2222-2222-222222222222"
    assert job.reserved_credits == 300


@respx.mock
def test_refresh_parses_status_snapshot(client: strand.Client) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    respx.get(f"{API_ROOT}/jobs/{job_id}").mock(
        return_value=Response(
            200,
            json={
                "id": job_id,
                "status": "running",
                "progress": 0.5,
                "reservedCredits": 300,
                "markers": ["CD3", "CD8"],
                "createdAt": "2026-05-14T10:00:00Z",
                "startedAt": "2026-05-14T10:00:30Z",
                "completedAt": None,
                "errorMessage": None,
                "resultsAvailable": False,
            },
        )
    )
    job = client.jobs.get(job_id)
    status = job.status
    assert status.status == "running"
    assert status.progress == 0.5
    assert status.markers == ["CD3", "CD8"]
    assert status.is_terminal is False
    # The status payload above doesn't include `model` — older deploys
    # didn't return it. `JobStatus.model` must surface as `None` rather
    # than KeyError'ing through the parser.
    assert status.model is None


@respx.mock
def test_refresh_surfaces_model_version_when_server_emits_it(
    client: strand.Client,
) -> None:
    """Post-PR-2 the platform always echoes the canonical v0.X label on
    job-shaped responses. The SDK surfaces it as `JobStatus.model` so
    downstream code (dashboards, audit logs in the SDK consumer's app)
    can read the actual version that ran without re-deriving from
    metadata."""
    job_id = "44444444-4444-4444-4444-444444444444"
    respx.get(f"{API_ROOT}/jobs/{job_id}").mock(
        return_value=Response(
            200,
            json={
                "id": job_id,
                "status": "completed",
                "progress": 1.0,
                "reservedCredits": 100,
                "markers": ["CD3"],
                "model": "v0.5",
                "createdAt": None,
                "startedAt": None,
                "completedAt": "2026-05-14T10:05:00Z",
                "errorMessage": None,
                "resultsAvailable": True,
            },
        )
    )
    job = client.jobs.get(job_id)
    assert job.status.model == "v0.5"


def _sse_stream(events: list[dict]) -> bytes:
    out: list[str] = []
    for e in events:
        out.append(f"data: {json.dumps(e)}\n\n")
    return "".join(out).encode("utf-8")


@respx.mock
def test_wait_via_sse_resolves_on_terminal_event(client: strand.Client) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": job_id, "reservedCredits": 100, "status": "queued"},
        )
    )
    stream_body = _sse_stream(
        [
            {"id": job_id, "status": "queued", "progress": None, "resultGcsPath": None},
            {"id": job_id, "status": "running", "progress": 0.5, "resultGcsPath": None},
            {
                "id": job_id,
                "status": "completed",
                "progress": 1.0,
                "resultGcsPath": "predictions/org/22222222",
            },
        ]
    )
    respx.get(f"{API_ROOT}/jobs/{job_id}/stream").mock(
        return_value=Response(
            200,
            content=stream_body,
            headers={"content-type": "text/event-stream"},
        )
    )
    # If wait drops down to polling, this lets the test still pass.
    respx.get(f"{API_ROOT}/jobs/{job_id}").mock(
        return_value=Response(
            200,
            json={
                "id": job_id,
                "status": "completed",
                "progress": 1.0,
                "reservedCredits": 100,
                "markers": ["CD3"],
                "createdAt": None,
                "startedAt": None,
                "completedAt": "2026-05-14T10:05:00Z",
                "errorMessage": None,
                "resultsAvailable": True,
            },
        )
    )

    job = client.predict.submit("upload-id", markers=["CD3"])
    status = job.wait(timeout=10, poll_interval=0.1)
    assert status.status == "completed"


@respx.mock
def test_cancel_flips_status_and_returns_handle(client: strand.Client) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    respx.post(f"{API_ROOT}/jobs/{job_id}/cancel").mock(
        return_value=Response(200, json={"id": job_id, "status": "cancelled"})
    )
    respx.get(f"{API_ROOT}/jobs/{job_id}").mock(
        return_value=Response(
            200,
            json={
                "id": job_id,
                "status": "cancelled",
                "progress": 0.5,
                "reservedCredits": 100,
                "markers": ["CD3"],
                "createdAt": "2026-05-14T10:00:00Z",
                "startedAt": "2026-05-14T10:00:30Z",
                "completedAt": "2026-05-14T10:02:00Z",
                "errorMessage": None,
                "resultsAvailable": False,
            },
        )
    )
    job = client.jobs.cancel(job_id)
    assert job.status.status == "cancelled"
    assert job.status.is_terminal is True


@respx.mock
def test_cancel_terminal_raises_bad_request(client: strand.Client) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    respx.post(f"{API_ROOT}/jobs/{job_id}/cancel").mock(
        return_value=Response(
            400,
            json={
                "error": "job_already_terminal",
                "message": "Job is already completed",
                "status": "completed",
            },
        )
    )
    with pytest.raises(strand.BadRequestError) as exc_info:
        client.jobs.cancel(job_id)
    assert exc_info.value.error_code == "job_already_terminal"


@respx.mock
def test_wait_raises_on_failed_status(client: strand.Client) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202, json={"jobId": job_id, "reservedCredits": 0, "status": "queued"}
        )
    )
    stream_body = _sse_stream(
        [{"id": job_id, "status": "failed", "progress": None, "resultGcsPath": None}]
    )
    respx.get(f"{API_ROOT}/jobs/{job_id}/stream").mock(
        return_value=Response(200, content=stream_body, headers={"content-type": "text/event-stream"})
    )
    respx.get(f"{API_ROOT}/jobs/{job_id}").mock(
        return_value=Response(
            200,
            json={
                "id": job_id,
                "status": "failed",
                "progress": None,
                "reservedCredits": 0,
                "markers": ["CD3"],
                "createdAt": None,
                "startedAt": None,
                "completedAt": "2026-05-14T10:05:00Z",
                "errorMessage": "kaboom",
                "resultsAvailable": False,
            },
        )
    )

    job = client.predict.submit("upload-id", markers=["CD3"])
    with pytest.raises(strand.JobFailedError) as exc_info:
        job.wait(timeout=10)
    assert exc_info.value.job_id == job_id


def _platform_root_meta(markers: list[str], *, include_he: bool = False) -> dict:
    """A LEGACY (unsharded, uncompressed) result root.

    Current results are `sharding_indexed` + zstd and their root carries no
    `H&E` entry — H&E lives on the sample prefix, and postprocess writes markers
    only. Those shapes are covered in `test_results.py` against metadata
    captured from production; this helper exists so the older layout keeps
    working, and it must not be taken as the current contract.
    """
    multiscales = []
    if include_he:
        multiscales.append(
            {
                "version": "0.5",
                "name": "H&E",
                "datasets": [{"path": "he/0"}],
            }
        )
    for m in markers:
        multiscales.append(
            {
                "version": "0.5",
                "name": m,
                "datasets": [{"path": f"markers/{m}/0"}],
            }
        )
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {"ome": {"version": "0.5"}, "multiscales": multiscales},
    }


def _array_meta(shape: list[int], chunk_shape: list[int], dtype: str = "float32") -> dict:
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "fill_value": 0,
    }


@respx.mock
def test_download_results_to_disk_walks_legacy_unsharded_zarr_tree(
    client: strand.Client, tmp_path: Path
) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    base = f"{API_ROOT}/jobs/{job_id}/results"

    respx.get(base).mock(
        return_value=Response(
            200,
            json={
                "resultUrl": "https://storage.googleapis.com/.../zarr.json?sig=...",
                "resultBasePath": "predictions/org/22222222",
                "expiresAt": "2026-05-14T11:05:00Z",
            },
        )
    )

    root_meta = _platform_root_meta(["CD3"], include_he=True)
    he_meta = _array_meta([3, 2, 2], [3, 2, 2], dtype="uint8")
    cd3_meta = _array_meta([1, 2, 2], [1, 2, 2], dtype="float32")
    he_chunk = struct.pack("<12B", *list(range(12)))
    cd3_chunk = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)

    respx.get(f"{base}/files/zarr.json").mock(
        return_value=Response(200, content=json.dumps(root_meta))
    )
    respx.get(f"{base}/files/he/0/zarr.json").mock(
        return_value=Response(200, content=json.dumps(he_meta))
    )
    respx.get(f"{base}/files/he/0/c/0/0/0").mock(
        return_value=Response(200, content=he_chunk)
    )
    respx.get(f"{base}/files/markers/CD3/0/zarr.json").mock(
        return_value=Response(200, content=json.dumps(cd3_meta))
    )
    respx.get(f"{base}/files/markers/CD3/0/c/0/0/0").mock(
        return_value=Response(200, content=cd3_chunk)
    )

    job = strand.Job(id=job_id, reserved_credits=None, client=client)
    target = tmp_path / "out"
    out = job.download_results(path=str(target))
    assert out == target
    assert (target / "zarr.json").exists()
    assert (target / "he" / "0" / "zarr.json").exists()
    assert (target / "markers" / "CD3" / "0" / "c" / "0" / "0" / "0").read_bytes() == cd3_chunk


@respx.mock
def test_download_results_to_anndata_from_legacy_unsharded_store(client: strand.Client) -> None:
    pytest.importorskip("anndata")
    pytest.importorskip("numpy")
    import numpy as np

    job_id = "22222222-2222-2222-2222-222222222222"
    base = f"{API_ROOT}/jobs/{job_id}/results"

    respx.get(base).mock(
        return_value=Response(
            200,
            json={
                "resultUrl": "https://example/zarr.json?sig",
                "resultBasePath": "predictions/org/22222222",
                "expiresAt": "2026-05-14T11:05:00Z",
            },
        )
    )

    root_meta = _platform_root_meta(["CD3", "CD8"], include_he=True)
    marker_meta = _array_meta([1, 2, 2], [1, 2, 2])
    he_meta = _array_meta([3, 2, 2], [3, 2, 2], dtype="uint8")
    cd3 = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    cd8 = struct.pack("<4f", 10.0, 20.0, 30.0, 40.0)
    he = struct.pack("<12B", *list(range(12)))

    respx.get(f"{base}/files/zarr.json").mock(
        return_value=Response(200, content=json.dumps(root_meta))
    )
    respx.get(f"{base}/files/he/0/zarr.json").mock(
        return_value=Response(200, content=json.dumps(he_meta))
    )
    respx.get(f"{base}/files/he/0/c/0/0/0").mock(
        return_value=Response(200, content=he)
    )
    respx.get(f"{base}/files/markers/CD3/0/zarr.json").mock(
        return_value=Response(200, content=json.dumps(marker_meta))
    )
    respx.get(f"{base}/files/markers/CD3/0/c/0/0/0").mock(
        return_value=Response(200, content=cd3)
    )
    respx.get(f"{base}/files/markers/CD8/0/zarr.json").mock(
        return_value=Response(200, content=json.dumps(marker_meta))
    )
    respx.get(f"{base}/files/markers/CD8/0/c/0/0/0").mock(
        return_value=Response(200, content=cd8)
    )

    job = strand.Job(id=job_id, reserved_credits=None, client=client)
    adata = job.download_results()
    # 2*2 pixels = 4 obs, 2 markers = 2 vars (H&E excluded by default).
    assert adata.shape == (4, 2)
    assert list(adata.var["channel"]) == ["CD3", "CD8"]
    assert "spatial" in adata.obsm
    assert np.array_equal(
        adata.obsm["spatial"],
        np.array([[0, 0], [1, 0], [0, 1], [1, 1]]),
    )


@respx.mock
def test_ome_tiff_export_status_methods(client: strand.Client) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    endpoint = f"{API_ROOT}/jobs/{job_id}/exports/ome-tiff"
    pending = {
        "status": "pending",
        "format": "ome-tiff",
        "sizeBytes": None,
        "updatedAt": "2026-07-30T10:00:00Z",
    }
    ready = {
        "status": "ready",
        "format": "ome-tiff",
        "sizeBytes": 1024,
        "downloadUrl": "https://storage.example/export.ome.tiff?signature=test",
        "downloadUrlExpiresAt": "2026-07-30T11:00:00Z",
        "updatedAt": "2026-07-30T10:05:00Z",
    }
    post = respx.post(endpoint).mock(return_value=Response(202, json=pending))
    get = respx.get(endpoint).mock(return_value=Response(200, json=ready))

    job = strand.Job(id=job_id, reserved_credits=None, client=client)
    requested = job.request_ome_tiff_export()
    current = job.get_ome_tiff_export()

    assert post.called
    assert get.called
    assert requested.status == "pending"
    assert current.status == "ready"
    assert current.size_bytes == 1024
    assert current.download_url_expires_at is not None


@respx.mock
def test_export_ome_tiff_waits_and_downloads(
    client: strand.Client, tmp_path: Path
) -> None:
    job_id = "22222222-2222-2222-2222-222222222222"
    endpoint = f"{API_ROOT}/jobs/{job_id}/exports/ome-tiff"
    download_url = "https://storage.example/export.ome.tiff?signature=test"
    respx.post(endpoint).mock(
        return_value=Response(
            202,
            json={
                "status": "running",
                "format": "ome-tiff",
                "sizeBytes": None,
                "updatedAt": None,
            },
        )
    )
    respx.get(endpoint).mock(
        return_value=Response(
            200,
            json={
                "status": "ready",
                "format": "ome-tiff",
                "sizeBytes": 4,
                "downloadUrl": download_url,
                "downloadUrlExpiresAt": "2026-07-30T11:00:00Z",
                "updatedAt": "2026-07-30T10:05:00Z",
            },
        )
    )
    download = respx.get(download_url).mock(return_value=Response(200, content=b"TIFF"))

    target = tmp_path / "nested" / "result.ome.tiff"
    job = strand.Job(id=job_id, reserved_credits=None, client=client)
    written = job.export_ome_tiff(str(target), timeout=1, poll_interval=0)

    assert download.called
    assert written == target
    assert target.read_bytes() == b"TIFF"
