"""Upload tests: resumable chunking, finalization, error paths."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import respx
from httpx import Response

import strand
from tests.conftest import API_ROOT


@respx.mock
def test_upload_file_chunks_and_finalizes(
    client: strand.Client, tmp_path: Path
) -> None:
    gcs_upload_url = "https://storage.googleapis.com/test/resumable?upload_id=abc"
    upload_id = "11111111-1111-1111-1111-111111111111"

    # 10 MB file → forces two 8 MB chunks (8 MB intermediate + 2 MB final).
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (10 * 1024 * 1024))

    respx.post(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "uploadId": upload_id,
                "uploadUrl": gcs_upload_url,
                "gcsPath": f"uploads/org/{upload_id}/slide.svs",
            },
        )
    )

    # GCS chunked PUT — intercept against the resumable URL.
    chunk_calls: list[tuple[str, int]] = []

    def _record(request):
        chunk_calls.append(
            (request.headers.get("content-range", ""), len(request.content))
        )
        # First chunk → 308; final → 200.
        rng = request.headers["content-range"]
        # parse "bytes A-B/C"
        end_total = rng.split("/")[1]
        end_byte = rng.split("-")[1].split("/")[0]
        is_final = end_total == str(int(end_byte) + 1)
        return Response(200 if is_final else 308)

    respx.put(gcs_upload_url).mock(side_effect=_record)

    respx.post(f"{API_ROOT}/uploads/{upload_id}/complete").mock(
        return_value=Response(
            200,
            json={
                "uploadId": upload_id,
                "status": "ready",
                "widthPx": 4096,
                "heightPx": 2048,
                "dimensionsSource": "sharp",
            },
        )
    )

    progress_events: list[tuple[int, int]] = []
    upload = client.uploads.upload_file(
        blob, progress=lambda done, total: progress_events.append((done, total))
    )

    assert upload.id == upload_id
    assert upload.width_px == 4096
    assert upload.height_px == 2048
    assert upload.status == "ready"
    assert len(chunk_calls) == 2  # 8 MB chunk + 2 MB final
    # Last progress callback equals total size.
    assert progress_events[-1] == (10 * 1024 * 1024, 10 * 1024 * 1024)


@respx.mock
def test_upload_rejects_invalid_chunk_size(
    client: strand.Client, tmp_path: Path
) -> None:
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x")
    with pytest.raises(ValueError, match="256 KiB"):
        client.uploads.upload_file(blob, chunk_size=12345)


@respx.mock
def test_upload_gcs_failure_raises_upload_error(
    client: strand.Client, tmp_path: Path
) -> None:
    upload_id = "11111111-1111-1111-1111-111111111111"
    gcs_upload_url = "https://storage.googleapis.com/test/resumable?upload_id=abc"
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * (256 * 1024))

    respx.post(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "uploadId": upload_id,
                "uploadUrl": gcs_upload_url,
                "gcsPath": f"uploads/org/{upload_id}/slide.svs",
            },
        )
    )
    respx.put(gcs_upload_url).mock(return_value=Response(503, text="GCS down"))

    with pytest.raises(strand.UploadError):
        client.uploads.upload_file(blob)


@respx.mock
def test_list_uploads_parses_rows(client: strand.Client) -> None:
    upload_id = "11111111-1111-4111-8111-111111111111"
    respx.get(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "uploads": [
                    {
                        "id": upload_id,
                        "filename": "a.svs",
                        "fileSize": "1024",
                        "status": "ready",
                        "gcsPath": f"uploads/org/{upload_id}/a.svs",
                        "createdAt": "2026-05-26T10:00:00Z",
                        "widthPx": 4096,
                        "heightPx": 2048,
                    },
                ],
                "nextCursor": None,
            },
        )
    )

    page = client.uploads.list()
    assert isinstance(page, strand.UploadList)
    assert page.next_cursor is None
    assert len(page.uploads) == 1
    [u] = page.uploads
    assert u.id == upload_id
    assert u.filename == "a.svs"
    assert u.file_size == 1024
    assert u.width_px == 4096
    assert u.height_px == 2048
    assert u.status == "ready"
    assert u.created_at is not None


@respx.mock
def test_list_uploads_forwards_limit_and_cursor(client: strand.Client) -> None:
    seen: list[dict[str, str]] = []

    def _record(request):
        seen.append(dict(request.url.params))
        return Response(200, json={"uploads": [], "nextCursor": None})

    respx.get(f"{API_ROOT}/uploads").mock(side_effect=_record)

    client.uploads.list(limit=25, cursor="opaque-cursor")
    assert seen == [{"limit": "25", "cursor": "opaque-cursor"}]


def test_list_uploads_rejects_non_positive_limit(client: strand.Client) -> None:
    with pytest.raises(ValueError):
        client.uploads.list(limit=0)


@respx.mock
def test_get_upload_returns_dataclass(client: strand.Client) -> None:
    upload_id = "22222222-2222-4222-8222-222222222222"
    respx.get(f"{API_ROOT}/uploads/{upload_id}").mock(
        return_value=Response(
            200,
            json={
                "id": upload_id,
                "filename": "b.svs",
                "fileSize": "2048",
                "status": "preprocessing",
                "gcsPath": f"uploads/org/{upload_id}/b.svs",
                "createdAt": "2026-05-26T11:00:00Z",
                "widthPx": None,
                "heightPx": None,
            },
        )
    )
    u = client.uploads.get(upload_id)
    assert u.id == upload_id
    assert u.filename == "b.svs"
    assert u.file_size == 2048
    assert u.status == "preprocessing"
    assert u.width_px is None


@respx.mock
def test_upload_if_not_exists_skips_upload_on_dedup_hit(
    client: strand.Client, tmp_path: Path
) -> None:
    """When the server says `existing: true`, we never touch GCS — we just
    return the existing row hydrated from the response."""
    blob = tmp_path / "slide.svs"
    payload = b"x" * (1024 * 1024)
    blob.write_bytes(payload)
    expected_hash = hashlib.sha256(payload).hexdigest()
    existing_id = "44444444-4444-4444-8444-444444444444"

    init_calls: list[dict[str, object]] = []

    def _init(request):
        init_calls.append({"body": request.read()})
        # Mirror the server's dedup-hit shape: serializeUpload-shaped row
        # plus uploadId + existing:true.
        return Response(
            200,
            json={
                "id": existing_id,
                "uploadId": existing_id,
                "existing": True,
                "filename": "slide.svs",
                "fileSize": str(len(payload)),
                "status": "ready",
                "gcsPath": f"uploads/org/{existing_id}/slide.svs",
                "createdAt": "2026-05-26T10:00:00Z",
                "widthPx": 4096,
                "heightPx": 2048,
            },
        )

    respx.post(f"{API_ROOT}/uploads").mock(side_effect=_init)
    # GCS PUT must NOT fire — wire a route that explodes if hit.
    gcs_route = respx.put(
        "https://storage.googleapis.com/test/resumable"
    ).mock(return_value=Response(500, text="should not be called"))

    upload = client.uploads.upload_file(blob, if_not_exists=True)

    assert upload.id == existing_id
    assert upload.status == "ready"
    assert upload.width_px == 4096
    assert upload.height_px == 2048
    # No GCS upload.
    assert gcs_route.call_count == 0
    # Init was called with the sha256 in the body.
    assert len(init_calls) == 1
    import json

    body = json.loads(init_calls[0]["body"])
    assert body["contentSha256"] == expected_hash
    assert body["filename"] == "slide.svs"
    assert body["fileSize"] == len(payload)


@respx.mock
def test_upload_if_not_exists_uploads_on_miss(
    client: strand.Client, tmp_path: Path
) -> None:
    """When the server says `existing: false`, the normal byte upload + complete
    path runs."""
    blob = tmp_path / "slide.svs"
    payload = b"y" * (256 * 1024)  # exactly one chunk
    blob.write_bytes(payload)
    expected_hash = hashlib.sha256(payload).hexdigest()
    upload_id = "55555555-5555-4555-8555-555555555555"
    gcs_upload_url = "https://storage.googleapis.com/test/resumable?upload_id=miss"

    init_bodies: list[bytes] = []

    def _init(request):
        init_bodies.append(request.read())
        return Response(
            200,
            json={
                "uploadId": upload_id,
                "uploadUrl": gcs_upload_url,
                "gcsPath": f"uploads/org/{upload_id}/slide.svs",
                "existing": False,
            },
        )

    respx.post(f"{API_ROOT}/uploads").mock(side_effect=_init)
    respx.put(gcs_upload_url).mock(return_value=Response(200))
    respx.post(f"{API_ROOT}/uploads/{upload_id}/complete").mock(
        return_value=Response(
            200,
            json={
                "uploadId": upload_id,
                "status": "preprocessing",
                "widthPx": 1024,
                "heightPx": 512,
                "dimensionsSource": "sharp",
            },
        )
    )

    upload = client.uploads.upload_file(blob, if_not_exists=True)

    assert upload.id == upload_id
    assert upload.status == "preprocessing"
    assert upload.width_px == 1024
    import json

    body = json.loads(init_bodies[0])
    assert body["contentSha256"] == expected_hash


@respx.mock
def test_upload_without_if_not_exists_omits_hash(
    client: strand.Client, tmp_path: Path
) -> None:
    """Default behavior: no sha256 computed, no `contentSha256` sent."""
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"z" * (256 * 1024))
    upload_id = "66666666-6666-4666-8666-666666666666"
    gcs_upload_url = "https://storage.googleapis.com/test/resumable?upload_id=none"

    init_bodies: list[bytes] = []

    def _init(request):
        init_bodies.append(request.read())
        return Response(
            200,
            json={
                "uploadId": upload_id,
                "uploadUrl": gcs_upload_url,
                "gcsPath": "p",
            },
        )

    respx.post(f"{API_ROOT}/uploads").mock(side_effect=_init)
    respx.put(gcs_upload_url).mock(return_value=Response(200))
    respx.post(f"{API_ROOT}/uploads/{upload_id}/complete").mock(
        return_value=Response(
            200,
            json={
                "uploadId": upload_id,
                "status": "preprocessing",
                "widthPx": 1,
                "heightPx": 1,
                "dimensionsSource": "stub",
            },
        )
    )

    client.uploads.upload_file(blob)

    import json

    body = json.loads(init_bodies[0])
    assert "contentSha256" not in body


@respx.mock
def test_get_upload_unknown_raises_not_found(client: strand.Client) -> None:
    upload_id = "33333333-3333-4333-8333-333333333333"
    respx.get(f"{API_ROOT}/uploads/{upload_id}").mock(
        return_value=Response(
            404, json={"error": "not_found", "message": "Upload not found"}
        )
    )
    with pytest.raises(strand.NotFoundError):
        client.uploads.get(upload_id)


# --- completion response shapes actually served in production -----------------
#
# Completion hands the sample to de-identification and returns no slide
# dimensions; they are read later off the de-identified copy. The tests above
# all mock a `widthPx`-bearing body, which is why `int(raw["widthPx"])` in
# `_with_completion` went unnoticed until a real upload raised KeyError.


def _mock_upload_flow(upload_id: str, gcs_upload_url: str, complete_body: dict) -> None:
    """Wire the three calls upload_file makes, with a caller-supplied completion."""
    respx.post(f"{API_ROOT}/uploads").mock(
        return_value=Response(
            200,
            json={
                "uploadId": upload_id,
                "uploadUrl": gcs_upload_url,
                "gcsPath": f"uploads/org/{upload_id}/slide.svs",
                "existing": False,
            },
        )
    )
    respx.put(gcs_upload_url).mock(return_value=Response(200))
    respx.post(f"{API_ROOT}/uploads/{upload_id}/complete").mock(
        return_value=Response(200, json=complete_body)
    )


@respx.mock
def test_complete_without_dimensions(client, tmp_path) -> None:
    upload_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    url = "https://storage.googleapis.com/upload/deid-1"
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * 512)

    _mock_upload_flow(upload_id, url, {"uploadId": upload_id, "status": "deid_running"})

    upload = client.uploads.upload_file(blob)

    assert upload.id == upload_id
    assert upload.status == "deid_running"
    # Not an error, and not zero — genuinely not known yet.
    assert upload.width_px is None
    assert upload.height_px is None


@respx.mock
def test_complete_is_idempotent_and_echoes_current_status(client, tmp_path) -> None:
    # A duplicate completion returns `skipped: true` with whatever status the
    # sample already had, and no dimensions.
    upload_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    url = "https://storage.googleapis.com/upload/deid-2"
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * 512)

    _mock_upload_flow(
        upload_id, url, {"uploadId": upload_id, "status": "preprocessing", "skipped": True}
    )

    upload = client.uploads.upload_file(blob)

    assert upload.status == "preprocessing"
    assert upload.width_px is None


@respx.mock
def test_complete_surfaces_deid_scheduling_failure(client, tmp_path) -> None:
    upload_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    url = "https://storage.googleapis.com/upload/deid-3"
    blob = tmp_path / "slide.svs"
    blob.write_bytes(b"x" * 512)

    _mock_upload_flow(
        upload_id,
        url,
        {
            "uploadId": upload_id,
            "status": "deid_failed",
            "warning": "Upload succeeded but de-identification could not be scheduled.",
        },
    )

    # The SDK does not raise here — the caller inspects status and decides.
    upload = client.uploads.upload_file(blob)
    assert upload.status == "deid_failed"
