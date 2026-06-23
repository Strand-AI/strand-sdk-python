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

    # Polling fallback (in case SSE drops mid-test). Echoes the canonical
    # v0.X label per design note §0 / §4 — this field is always a live
    # v0.X id (or a historical sunset id like `"v0.1"` on old rows from
    # the legacy 35-marker base; see the 2026-06-03 §8.2 renumber).
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": JOB_ID,
                "status": "completed",
                "progress": 1.0,
                "reservedCredits": 42,
                "markers": markers,
                "model": "v0.5",
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
    # `PredictResult.model` echoes the canonical v0.X label the platform
    # persisted — never a legacy alias, never None on a fresh response.
    # This is the §0 hard constraint manifesting on the SDK return type.
    assert result.model == "v0.5"
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

    stages: list[tuple[str, float]] = []
    client.predict(
        blob,
        markers=["CD3"],
        output_dir=out,
        poll_interval_sec=0.05,
        timeout_sec=10,
        on_progress=lambda stage, frac: stages.append((stage, frac)),
    )

    # Every stage must be reported with a float — never None — so the callback
    # contract can rely on `frac` being a real number.
    for stage, frac in stages:
        assert isinstance(frac, float), f"stage {stage!r} received non-float frac={frac!r}"
        assert 0.0 <= frac <= 1.0, f"frac out of range for {stage}: {frac}"

    seen_stages = {s for s, _ in stages}
    assert {"upload", "submit", "wait", "download"} <= seen_stages
    # Each stage must bracket with 0.0 at start and 1.0 at end.
    for target in ("upload", "submit", "wait", "download"):
        fracs = [f for s, f in stages if s == target]
        assert fracs[0] == 0.0, f"stage {target} did not start with 0.0: {fracs}"
        assert fracs[-1] == 1.0, f"stage {target} did not end with 1.0: {fracs}"


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
    # Upload had already completed successfully — recovery path: the upload_id
    # must be attached to the error so callers can resubmit without re-uploading.
    assert exc_info.value.upload_id == UPLOAD_ID


@respx.mock
def test_predict_attaches_upload_id_on_submit_failure(
    client: strand.Client, tmp_path: Path
) -> None:
    """If submit fails *after* upload completes, upload_id must surface on the error."""
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
    # Submit fails with 402 — insufficient credits.
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            402,
            json={"error": "insufficient_credits", "message": "Need 500 credits", "required": 500},
        )
    )

    with pytest.raises(strand.InsufficientCreditsError) as exc_info:
        client.predict(blob, markers=["CD3"], poll_interval_sec=0.05, timeout_sec=10)
    assert exc_info.value.upload_id == UPLOAD_ID
    assert exc_info.value.required == 500


@respx.mock
def test_predict_submit_maps_unknown_markers(client: strand.Client) -> None:
    """Submitting unknown markers surfaces as UnknownMarkerError with the offending names."""
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            400,
            json={
                "error": "unknown_markers",
                "message": "Unknown markers: MysteryMarker, AnotherFake",
                "unknownMarkers": ["MysteryMarker", "AnotherFake"],
                "knownMarkersSample": ["CD3e", "CD4", "CD8", "Ki67"],
            },
        )
    )

    with pytest.raises(strand.UnknownMarkerError) as exc_info:
        client.predict.submit("upload-id", markers=["CD3e", "MysteryMarker", "AnotherFake"])

    err = exc_info.value
    assert err.unknown == ["MysteryMarker", "AnotherFake"]
    assert err.known_subset is not None
    assert "CD3e" in err.known_subset
    # UnknownMarkerError is still a BadRequestError, so generic catch-all works too.
    assert isinstance(err, strand.BadRequestError)


@respx.mock
def test_predict_estimate_maps_unknown_markers(client: strand.Client) -> None:
    respx.post(f"{API_ROOT}/predict/estimate").mock(
        return_value=Response(
            400,
            json={
                "error": "unknown_markers",
                "message": "Unknown marker: Bogus",
                "unknownMarkers": ["Bogus"],
            },
        )
    )

    with pytest.raises(strand.UnknownMarkerError) as exc_info:
        client.predict.estimate("upload-id", markers=["Bogus"])
    assert exc_info.value.unknown == ["Bogus"]
    assert exc_info.value.known_subset is None


def test_predict_validates_markers(client: strand.Client, tmp_path: Path) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x")
    with pytest.raises(ValueError, match="at least one"):
        client.predict(blob, markers=[])


def test_predict_missing_file_raises(client: strand.Client, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        client.predict(tmp_path / "missing.svs", markers=["CD3"])


@respx.mock
def test_predict_submit_passes_model_when_provided(client: strand.Client) -> None:
    """`model=` is forwarded as `model` on the wire body — canonical v0.X."""
    route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 7, "status": "queued"},
        )
    )

    client.predict.submit(UPLOAD_ID, markers=["CD3"], model="v0.5")
    sent = json.loads(route.calls[0].request.content)
    assert sent["uploadId"] == UPLOAD_ID
    assert sent["markers"] == ["CD3"]
    assert sent["model"] == "v0.5"


@respx.mock
def test_predict_submit_accepts_canonical_v0p4(client: strand.Client) -> None:
    """v0.4 is the other live canonical id — also forwarded verbatim."""
    route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 7, "status": "queued"},
        )
    )

    client.predict.submit(UPLOAD_ID, markers=["CD3"], model="v0.4")
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "v0.4"


@respx.mock
def test_predict_submit_forwards_unknown_model_strings_verbatim(
    client: strand.Client,
) -> None:
    """The SDK no longer rewrites legacy `"v10*"` aliases — that map was
    dropped on 2026-06-03 (design note §4, rewritten). Unknown strings,
    including the legacy aliases, are forwarded as-is and the server
    decides the error code (400 `unknown_model`). This keeps the SDK
    forward-compatible with new versions the server picks up before the
    SDK ships a release, and gives callers a single coherent error path
    instead of a SDK-side ValueError plus a server error on the next try."""
    route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            400,
            json={
                "error": "unknown_model",
                "message": "Unknown model: v10-fullpanel-v2",
            },
        )
    )

    with pytest.raises(strand.BadRequestError):
        client.predict.submit(UPLOAD_ID, markers=["CD3"], model="v10-fullpanel-v2")
    # The wire body carries the original string — no client-side rewrite.
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "v10-fullpanel-v2"


@respx.mock
def test_predict_submit_unknown_model_passes_through_to_server(
    client: strand.Client,
) -> None:
    """An unknown string is forwarded verbatim — no SDK-side warning,
    no client-side validation. The server returns 400 unknown_model.

    This keeps the SDK forward-compatible with new Prism versions
    added on the server without a SDK release."""
    route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            400,
            json={
                "error": "unknown_model",
                "message": "Unknown model: v0.99",
            },
        )
    )

    with pytest.raises(strand.BadRequestError):
        client.predict.submit(UPLOAD_ID, markers=["CD3"], model="v0.99")
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "v0.99"


@respx.mock
def test_predict_submit_omits_model_field_when_unspecified(client: strand.Client) -> None:
    """No `model` key is sent when caller doesn't pass one — backend default applies."""
    route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 7, "status": "queued"},
        )
    )

    client.predict.submit(UPLOAD_ID, markers=["CD3"])
    sent = json.loads(route.calls[0].request.content)
    assert "model" not in sent


@respx.mock
def test_predict_wait_false_returns_job_after_submit(
    client: strand.Client, tmp_path: Path
) -> None:
    """`wait=False` returns a Job once upload + submit complete, skipping wait/download."""
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
    submit_route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 42, "status": "queued"},
        )
    )
    # Deliberately mock /jobs/{id}/stream so that a stray .wait() in the test
    # path would surface as a test failure — proves wait/download didn't fire.
    wait_route = respx.get(f"{API_ROOT}/jobs/{JOB_ID}/stream")
    results_route = respx.get(f"{API_ROOT}/jobs/{JOB_ID}/results")

    stages: list[tuple[str, float]] = []
    job = client.predict(
        blob,
        markers=["CD3"],
        wait=False,
        on_progress=lambda s, f: stages.append((s, f)),
    )

    assert isinstance(job, strand.Job)
    assert job.id == JOB_ID
    assert job.reserved_credits == 42
    assert submit_route.called
    # No wait / download in fire-and-forget mode.
    assert not wait_route.called
    assert not results_route.called
    # Progress stages must include upload + submit, but NOT wait/download.
    seen = {s for s, _ in stages}
    assert "upload" in seen
    assert "submit" in seen
    assert "wait" not in seen
    assert "download" not in seen


@respx.mock
def test_predict_wait_false_forwards_model(
    client: strand.Client, tmp_path: Path
) -> None:
    """`model=` is plumbed through the wait=False path too."""
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
    submit_route = respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 0, "status": "queued"},
        )
    )

    client.predict(blob, markers=["CD3"], model="v0.4", wait=False)
    sent = json.loads(submit_route.calls[0].request.content)
    assert sent["model"] == "v0.4"


@respx.mock
def test_predict_sends_content_sha256_for_dedup(
    client: strand.Client, tmp_path: Path
) -> None:
    """`predict()` must call uploads with `if_not_exists=True` so the platform
    can dedup repeat calls on the same WSI instead of re-uploading + racing
    the still-preprocessing prior upload on submit(). Regression for #95."""
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))

    init_bodies: list[bytes] = []

    def _init(request):
        init_bodies.append(request.read())
        return Response(
            200,
            json={
                "uploadId": UPLOAD_ID,
                "uploadUrl": GCS_URL,
                "gcsPath": f"uploads/org/{UPLOAD_ID}/slide.svs",
                "existing": False,
            },
        )

    respx.post(f"{API_ROOT}/uploads").mock(side_effect=_init)
    _mock_full_pipeline(["CD3"])
    # Re-mock /uploads now that _mock_full_pipeline replaced it with a
    # version that doesn't capture bodies.
    respx.post(f"{API_ROOT}/uploads").mock(side_effect=_init)

    client.predict(blob, markers=["CD3"], poll_interval_sec=0.05, timeout_sec=10)

    body = json.loads(init_bodies[0])
    # The presence of contentSha256 is what proves if_not_exists=True was
    # passed down — that's the only path that hashes the file.
    assert "contentSha256" in body, (
        "predict() must request server-side dedup by sending contentSha256"
    )


@respx.mock
def test_predict_skips_reupload_on_dedup_hit(
    client: strand.Client, tmp_path: Path
) -> None:
    """End-to-end: server reports `existing: true` → predict() proceeds straight
    to submit/wait/download without touching GCS or /uploads/{id}/complete."""
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))

    # Dedup hit — server returns the existing row's GET shape.
    respx.post(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "id": UPLOAD_ID,
                "uploadId": UPLOAD_ID,
                "existing": True,
                "filename": "slide.svs",
                "fileSize": "262144",
                "status": "ready",
                "gcsPath": f"uploads/org/{UPLOAD_ID}/slide.svs",
                "createdAt": "2026-05-26T10:00:00Z",
                "widthPx": 1024,
                "heightPx": 1024,
            },
        )
    )
    # GCS PUT and /complete must NOT fire — wire routes that fail if hit.
    gcs_route = respx.put(GCS_URL).mock(
        return_value=Response(500, text="should not be called")
    )
    complete_route = respx.post(f"{API_ROOT}/uploads/{UPLOAD_ID}/complete").mock(
        return_value=Response(500, text="should not be called")
    )
    # Submit/wait/download still run as normal.
    respx.post(f"{API_ROOT}/predict").mock(
        return_value=Response(
            202,
            json={"jobId": JOB_ID, "reservedCredits": 42, "status": "queued"},
        )
    )
    sse_body = (
        f'data: {{"id":"{JOB_ID}","status":"completed","progress":1.0,'
        f'"resultGcsPath":"{RESULT_BASE}"}}\n\n'
    ).encode()
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}/stream").mock(
        return_value=Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )
    )
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}").mock(
        return_value=Response(
            200,
            json={
                "id": JOB_ID,
                "status": "completed",
                "progress": 1.0,
                "reservedCredits": 42,
                "markers": ["CD3"],
                "createdAt": None,
                "startedAt": None,
                "completedAt": "2026-05-20T10:05:00Z",
                "errorMessage": None,
                "resultsAvailable": True,
            },
        )
    )
    respx.get(f"{API_ROOT}/jobs/{JOB_ID}/results").mock(
        return_value=Response(
            200,
            json={
                "resultUrl": "https://storage.googleapis.com/.../zarr.json?sig=...",
                "resultBasePath": RESULT_BASE,
                "expiresAt": "2026-05-20T11:05:00Z",
            },
        )
    )

    result = client.predict(
        blob, markers=["CD3"], poll_interval_sec=0.05, timeout_sec=10
    )

    assert result.job_id == JOB_ID
    assert result.status == "completed"
    assert gcs_route.call_count == 0
    assert complete_route.call_count == 0
