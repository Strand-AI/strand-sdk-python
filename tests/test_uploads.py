"""Upload tests: resumable chunking, finalization, error paths."""

from __future__ import annotations

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
def test_get_upload_unknown_raises_not_found(client: strand.Client) -> None:
    upload_id = "33333333-3333-4333-8333-333333333333"
    respx.get(f"{API_ROOT}/uploads/{upload_id}").mock(
        return_value=Response(
            404, json={"error": "not_found", "message": "Upload not found"}
        )
    )
    with pytest.raises(strand.NotFoundError):
        client.uploads.get(upload_id)
