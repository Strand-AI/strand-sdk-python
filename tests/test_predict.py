"""End-to-end test for the `client.predict(...)` convenience method.

Mocks all five REST endpoints + the GCS resumable PUT so the orchestrator
exercises upload → submit → wait → download in one go without a live backend.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
import respx
from httpx import Response

import strand
from tests.conftest import API_ROOT

JOB_ID = "33333333-3333-3333-3333-333333333333"
UPLOAD_ID = "11111111-1111-1111-1111-111111111111"
GCS_URL = "https://storage.googleapis.com/test/resumable?upload_id=abc"
RESULT_BASE = "predictions/org/33333333"


def _array_meta(shape: list[int], chunk: list[int], dtype: str = "float32") -> dict:
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "fill_value": 0,
    }


def _root_meta(markers: list[str]) -> dict:
    multiscales = [{"version": "0.5", "name": "H&E", "datasets": [{"path": "he/0"}]}]
    for m in markers:
        multiscales.append(
            {"version": "0.5", "name": m, "datasets": [{"path": f"markers/{m}/0"}]}
        )
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {"ome": {"version": "0.5"}, "multiscales": multiscales},
    }


def _mock_full_pipeline(markers: list[str]) -> None:
    """Wire respx mocks for the entire predict() flow."""
    respx.post(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "uploadId": UPLOAD_ID,
                "uploadUrl": GCS_URL,
                "gcsPath": f"uploads/org/{UPLOAD_ID}/slide.svs",
            },
        )
    )

    def _gcs_put(request):
        rng = request.headers["content-range"]
        end_total = rng.split("/")[1]
        end_byte = rng.split("-")[1].split("/")[0]
        is_final = end_total == str(int(end_byte) + 1)
        return Response(200 if is_final else 308)

    respx.put(GCS_URL).mock(side_effect=_gcs_put)

    respx.post(f"{API_ROOT}/uploads/{UPLOAD_ID}/complete").mock(
        return_value=Response(
            200,
            json={
                "uploadId": UPLOAD_ID,
                "status": "ready",
                "widthPx": 1024,
                "heightPx": 1024,
                "dimensionsSource": "sharp",
            },
        )
    )

    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 42, "status": "queued"},
        )
    )

    # SSE stream → emit a single terminal event.
    sse_body = (
        f'data: {{"id":"{JOB_ID}","status":"completed","progress":1.0,'
        f'"resultGcsPath":"{RESULT_BASE}"}}\n\n'
    ).encode()
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}/stream").mock(
        return_value=Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    # Polling fallback (in case SSE drops mid-test).
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": JOB_ID,
                "status": "completed",
                "progress": 1.0,
                "reservedCredits": 42,
                "markers": markers,
                "createdAt": None,
                "startedAt": None,
                "completedAt": "2026-05-20T10:05:00Z",
                "errorMessage": None,
                "resultsAvailable": True,
            },
        )
    )

    base = f"{API_ROOT}/jobs/{JOB_ID}/results"
    respx.get(base).mock(
        return_value=Response(
            200,
            json={
                "resultUrl": "https://storage.googleapis.com/.../zarr.json?sig=...",
                "resultBasePath": RESULT_BASE,
                "expiresAt": "2026-05-20T11:05:00Z",
            },
        )
    )

    root = _root_meta(markers)
    he_meta = _array_meta([3, 2, 2], [3, 2, 2], dtype="uint8")
    marker_meta = _array_meta([1, 2, 2], [1, 2, 2])
    he_chunk = struct.pack("<12B", *range(12))
    marker_chunk = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)

    respx.get(f"{base}/files/zarr.json").mock(
        return_value=Response(200, content=json.dumps(root))
    )
    respx.get(f"{base}/files/he/0/zarr.json").mock(
        return_value=Response(200, content=json.dumps(he_meta))
    )
    respx.get(f"{base}/files/he/0/c/0/0/0").mock(
        return_value=Response(200, content=he_chunk)
    )
    for m in markers:
        respx.get(f"{base}/files/markers/{m}/0/zarr.json").mock(
            return_value=Response(200, content=json.dumps(marker_meta))
        )
        respx.get(f"{base}/files/markers/{m}/0/c/0/0/0").mock(
            return_value=Response(200, content=marker_chunk)
        )


@respx.mock
def test_predict_full_pipeline_writes_files(
    client: strand.Client, tmp_path: Path
) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))  # one 256 KiB chunk → single final PUT
    out = tmp_path / "out"

    markers = ["CD3", "CD8"]
    _mock_full_pipeline(markers)

    result = client.predict(
        blob, markers=markers, output_dir=out, poll_interval_sec=0.05, timeout_sec=10
    )

    assert isinstance(result, strand.PredictResult)
    assert result.job_id == JOB_ID
    assert result.status == "completed"
    assert result.credits_used == 42
    assert result.output_dir == out
    assert set(result.marker_outputs.keys()) == {"CD3", "CD8"}
    assert result.marker_outputs["CD3"] == out / "markers" / "CD3"
    # Whole zarr store was mirrored.
    assert (out / "zarr.json").exists()
    assert (out / "markers" / "CD3" / "0" / "c" / "0" / "0" / "0").exists()
    assert (out / "markers" / "CD8" / "0" / "c" / "0" / "0" / "0").exists()


@respx.mock
def test_predict_without_output_dir_skips_download(
    client: strand.Client, tmp_path: Path
) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))

    markers = ["CD3"]
    _mock_full_pipeline(markers)

    result = client.predict(
        blob, markers=markers, poll_interval_sec=0.05, timeout_sec=10
    )

    assert result.status == "completed"
    assert result.output_dir is None
    assert result.marker_outputs == {}
    # The JobResults handle is still available for on-demand reads.
    assert result.results is not None
    assert result.results.job_id == JOB_ID


@respx.mock
def test_predict_reports_progress_stages(
    client: strand.Client, tmp_path: Path
) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))
    out = tmp_path / "out"

    _mock_full_pipeline(["CD3"])

    stages: list[tuple[str, float | None]] = []
    client.predict(
        blob,
        markers=["CD3"],
        output_dir=out,
        poll_interval_sec=0.05,
        timeout_sec=10,
        on_progress=lambda stage, frac: stages.append((stage, frac)),
    )

    seen_stages = {s for s, _ in stages}
    assert {"upload", "submit", "wait", "download"} <= seen_stages


@respx.mock
def test_predict_propagates_job_failure(
    client: strand.Client, tmp_path: Path
) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))

    respx.post(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "uploadId": UPLOAD_ID,
                "uploadUrl": GCS_URL,
                "gcsPath": f"uploads/org/{UPLOAD_ID}/slide.svs",
            },
        )
    )
    respx.put(GCS_URL).mock(return_value=Response(200))
    respx.post(f"{API_ROOT}/uploads/{UPLOAD_ID}/complete").mock(
        return_value=Response(
            200,
            json={
                "uploadId": UPLOAD_ID,
                "status": "ready",
                "widthPx": 8,
                "heightPx": 8,
                "dimensionsSource": "sharp",
            },
        )
    )
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 0, "status": "queued"},
        )
    )
    sse = (
        f'data: {{"id":"{JOB_ID}","status":"failed","progress":null,'
        f'"resultGcsPath":null}}\n\n'
    ).encode()
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}/stream").mock(
        return_value=Response(200, content=sse, headers={"content-type": "text/event-stream"})
    )
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": JOB_ID,
                "status": "failed",
                "progress": None,
                "reservedCredits": 0,
                "markers": ["CD3"],
                "createdAt": None,
                "startedAt": None,
                "completedAt": "2026-05-20T10:05:00Z",
                "errorMessage": "model OOM",
                "resultsAvailable": False,
            },
        )
    )

    with pytest.raises(strand.JobFailedError) as exc_info:
        client.predict(blob, markers=["CD3"], poll_interval_sec=0.05, timeout_sec=10)
    assert exc_info.value.job_id == JOB_ID


def test_predict_validates_markers(client: strand.Client, tmp_path: Path) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x")
    with pytest.raises(ValueError, match="at least one"):
        client.predict(blob, markers=[])


def test_predict_missing_file_raises(client: strand.Client, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        client.predict(tmp_path / "missing.svs", markers=["CD3"])
