"""Uploads namespace.

Handles the event-driven resumable flow:
  1. POST /api/v1/uploads             → create session
  2. PUT  <uploadUrl> w/ Content-Range → stream chunks directly to GCS
  3. Poll GET /api/v1/uploads/{id} until GCS OBJECT_FINALIZE starts ingest

The chunked PUT bypasses the platform; we hit GCS directly using the
resumable session URL it returns. Chunks must be a multiple of 256 KiB
per GCS spec, except the last chunk; we use 8 MiB.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._errors import UploadError
from ._models import Upload
from ._samples import _validate_mpp

if TYPE_CHECKING:
    from ._http import HttpSession


@dataclass(frozen=True, slots=True)
class UploadList:
    """One page of `client.uploads.list()` results."""

    uploads: list[Upload]
    next_cursor: str | None


CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB — GCS resumable requires multiples of 256 KiB.
DEFAULT_CONTENT_TYPE = "application/octet-stream"

# Map common WSI extensions to a reasonable Content-Type the platform can record.
_CONTENT_TYPE_BY_EXT = {
    ".svs": "image/aperio-svs",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".ndpi": "image/ndpi",
    ".scn": "image/scn",
    ".mrxs": "image/mrxs",
    ".vsi": "image/vsi",
    ".bif": "image/bif",
}


ProgressCb = Callable[[int, int], None]

# Read size for streaming sha256. Bigger than CHUNK_SIZE is fine — we're not
# bound by GCS multiples here; 1 MiB is a good cache-friendly value.
_HASH_READ_SIZE = 1024 * 1024
_INGEST_POLL_INTERVAL_SECONDS = 0.5
_INGEST_START_TIMEOUT_SECONDS = 120.0


def _normalize_mpp(mpp: float | tuple[float, float] | None) -> float | None:
    """Collapse user-reported MPP to the isotropic scalar the API expects.

    Slides are isotropic — an ``(x, y)`` tuple is accepted for callers whose
    metadata carries per-axis values, but the axes must be equal. Bounds
    (> 0, <= 100 µm/px) mirror the server's canonical validator.
    """
    if mpp is None:
        return None
    if isinstance(mpp, tuple):
        if len(mpp) != 2:
            raise ValueError("mpp tuple must be (x, y)")
        x = _validate_mpp(mpp[0], "mpp[0]")
        y = _validate_mpp(mpp[1], "mpp[1]")
        if x != y:
            raise ValueError("Slides are isotropic: mpp x and y must be equal")
        return x
    return _validate_mpp(mpp, "mpp")


def _sha256_of_file(path: Path) -> str:
    """Streaming sha256 hex digest of a file. Never buffers the whole file."""
    # We always drive the loop ourselves. hashlib.file_digest landed in 3.11
    # and would be marginally faster, but we still support 3.10 and an
    # explicit loop dodges the cross-version typing skew (file_digest's
    # return is `Any` on stubs, which trips mypy strict).
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_READ_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class Uploads:
    """Public uploads namespace exposed on `Client.uploads`."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def list(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> UploadList:
        """List uploads for the calling org, newest-first.

        Args:
            limit: Page size (1-200). Defaults to 100.
            cursor: Opaque cursor from a prior response's `next_cursor`.

        Returns:
            `UploadList(uploads=[...], next_cursor=...)`. `next_cursor` is `None`
            on the last page.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        raw = self._http.request_json("GET", "/uploads", params=params)
        items = raw.get("uploads") or []
        return UploadList(
            uploads=[Upload._from_row(row) for row in items],
            next_cursor=raw.get("nextCursor"),
        )

    def get(self, upload_id: str) -> Upload:
        """Fetch a single upload by id.

        Raises:
            NotFoundError: If no upload with that id exists for the calling org.
        """
        raw = self._http.request_json("GET", f"/uploads/{upload_id}")
        return Upload._from_row(raw)

    def create_session(
        self,
        *,
        filename: str,
        file_size: int,
        content_type: str | None = None,
        auto_segment: bool | None = None,
        mpp: float | tuple[float, float] | None = None,
    ) -> Upload:
        """Create a resumable upload session for a client that already holds the
        bytes (an autonomous agent, not a local file path).

        This is step 1 of the resumable flow WITHOUT the byte stream: it mints
        the session and returns an `Upload` whose `upload_url` is the resumable
        target the caller PUTs the slide bytes to directly, plus `id` (the
        `upload_id`). The caller must supply an already-de-identified slide.
        After the bytes land, GCS automatically starts ingest preprocessing;
        optional defense-in-depth de-identification runs only when enabled for
        the organization.
        Poll `get(upload_id)` until the status leaves `uploading`. Contrast with
        `upload_file`, which streams
        a local path and performs that short ingest-start wait in one call.

        The session (and the object key it writes to) is bound to the calling
        org by the server; there is no way to redirect it to another org.

        Args:
            filename: Slide filename (used for the stored object + Content-Type
                inference).
            file_size: Total size of the slide in bytes. Must be positive.
            content_type: Override the MIME type. Defaults to a type inferred
                from the filename extension.
            auto_segment: Per-upload auto-segmentation override. ``None``
                (default) uses the org default; ``False`` skips segmentation
                (the slide is still ingested and rendered); ``True`` forces it on.
            mpp: User-reported microns per pixel (isotropic), persisted at
                creation. Pass a float, or an ``(x, y)`` tuple whose values are
                equal. Must be > 0 and <= 100.

        Returns:
            `Upload` with `id`, `upload_url`, and `gcs_path` populated.
        """
        if file_size <= 0:
            raise ValueError("file_size must be positive")
        ct = content_type or _CONTENT_TYPE_BY_EXT.get(
            Path(filename).suffix.lower(), DEFAULT_CONTENT_TYPE
        )
        mpp_value = _normalize_mpp(mpp)
        # content_sha256 is None: the agent supplies the bytes, so we don't
        # dedup here (that path needs a client-computed hash of a local file).
        session, _existing = self._initiate(filename, file_size, ct, None, auto_segment, mpp_value)
        return session

    def upload_file(
        self,
        path: str | os.PathLike[str],
        *,
        content_type: str | None = None,
        chunk_size: int = CHUNK_SIZE,
        progress: ProgressCb | None = None,
        if_not_exists: bool = False,
        auto_segment: bool | None = None,
        mpp: float | tuple[float, float] | None = None,
    ) -> Upload:
        """Upload a local WSI file end-to-end.

        Streams in 8 MiB chunks via the GCS resumable session URL returned by
        `POST /api/v1/uploads`. GCS OBJECT_FINALIZE starts ingest automatically;
        this method polls the upload resource until that event is accepted.

        Args:
            path: Local, already-de-identified slide file to upload. Do not upload PHI
                or other regulated identifiable health information.
            content_type: Override the auto-detected MIME type.
            chunk_size: Bytes per PUT request. Must be a positive multiple of 256 KiB
                except for the last chunk. Defaults to 8 MiB.
            progress: Optional `(bytes_uploaded, total_bytes)` callback.
            if_not_exists: When True, sha256 the file (streaming, ~1-2s per 600 MiB)
                and ask the server to dedup against existing non-archived samples
                with the same content hash. On a hit, the byte upload is skipped
                and the existing `Upload` is returned. Defaults to False.
            auto_segment: Opt out of automatic cell segmentation for this upload.
                `None` (default) uses the org's default; `False` skips segmentation
                (the slide is still ingested and rendered); `True` forces it on even
                when the org default is off.
            mpp: User-reported microns per pixel, for callers that already know
                their slide's scale. Persisted on the sample at creation and takes
                precedence over the slide's own calibrated value, so the sample is
                predict-ready as soon as preprocessing finishes — no follow-up
                `samples.patch(..., mpp=...)` needed. Slides are isotropic: pass a float,
                or an `(x, y)` tuple whose values are equal. Must be > 0 and
                <= 100. Ignored on an `if_not_exists` dedup hit (the existing
                sample's scale stands).

        Returns:
            `Upload` after the storage event has advanced it beyond `uploading`
            (normally `deid_running`, `preprocessing`, or `ready`). Dimensions
            may still be absent while ingest preprocessing runs. When
            `if_not_exists=True` and the server reports a dedup hit, the
            existing row is returned as-is.

        Raises:
            FileNotFoundError: If `path` does not exist.
            UploadError: If the GCS resumable session returns an unexpected status.
        """
        local = Path(path)
        if not local.is_file():
            raise FileNotFoundError(f"No such file: {local}")
        if chunk_size <= 0 or chunk_size % (256 * 1024) != 0:
            raise ValueError("chunk_size must be a positive multiple of 256 KiB (262144 bytes).")

        size = local.stat().st_size
        ct = content_type or _CONTENT_TYPE_BY_EXT.get(local.suffix.lower(), DEFAULT_CONTENT_TYPE)

        mpp_value = _normalize_mpp(mpp)
        content_sha256 = _sha256_of_file(local) if if_not_exists else None

        session, existing = self._initiate(
            local.name, size, ct, content_sha256, auto_segment, mpp_value
        )
        if existing:
            # Server confirmed a non-archived row already holds this content
            # hash — skip the byte upload entirely and surface the existing
            # row to the caller.
            return session
        # _from_create always sets upload_url for the fresh-upload branch;
        # narrow for mypy after widening the dataclass to support list/get
        # rows where upload_url is None.
        assert session.upload_url is not None
        self._stream_to_gcs(
            session.upload_url, local, size, ct, chunk_size=chunk_size, progress=progress
        )
        return self._wait_for_ingest_start(session)

    # ---------- internal helpers ----------

    def _initiate(
        self,
        filename: str,
        size: int,
        content_type: str,
        content_sha256: str | None,
        auto_segment: bool | None = None,
        mpp: float | None = None,
    ) -> tuple[Upload, bool]:
        body: dict[str, Any] = {
            "filename": filename,
            "fileSize": size,
            "contentType": content_type,
        }
        if content_sha256 is not None:
            body["contentSha256"] = content_sha256
        if auto_segment is not None:
            body["autoSegment"] = auto_segment
        if mpp is not None:
            body["mpp"] = mpp
        raw = self._http.request_json("POST", "/uploads", json=body)
        existing = bool(raw.get("existing"))
        if existing:
            # Dedup hit — server returns the GET-by-id shape so we can hydrate
            # via _from_row. `uploadId` is duplicated alongside `id` for parity
            # with the create-response convention.
            return Upload._from_row(raw), True
        return Upload._from_create(raw), False

    def _wait_for_ingest_start(self, session: Upload) -> Upload:
        deadline = time.monotonic() + _INGEST_START_TIMEOUT_SECONDS
        while True:
            current = self.get(session.id)
            if current.status == "upload_failed":
                raise UploadError(
                    "GCS finalized an object whose size did not match the declared upload; "
                    "create a new upload session and send the complete file.",
                    error_code="upload_failed",
                    upload_id=session.id,
                )
            if current.status != "uploading":
                return replace(current, upload_url=session.upload_url)
            if time.monotonic() >= deadline:
                raise UploadError(
                    "The upload reached GCS, but automatic ingest did not start within 120 seconds.",
                    error_code="ingest_start_timeout",
                    upload_id=session.id,
                )
            time.sleep(_INGEST_POLL_INTERVAL_SECONDS)

    def _stream_to_gcs(
        self,
        upload_url: str,
        path: Path,
        size: int,
        content_type: str,
        *,
        chunk_size: int,
        progress: ProgressCb | None,
    ) -> None:
        # Use a fresh httpx.Client — we're talking to GCS, not our API.
        with (
            httpx.Client(
                timeout=httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0)
            ) as gcs,
            path.open("rb") as fh,
        ):
            pos = 0
            while pos < size:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                end = pos + len(chunk) - 1
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {pos}-{end}/{size}",
                    "Content-Type": content_type,
                }
                resp = gcs.put(upload_url, content=chunk, headers=headers)
                if pos + len(chunk) >= size:
                    # Final chunk — GCS returns 200/201.
                    if resp.status_code not in (200, 201):
                        raise UploadError(
                            f"GCS rejected final chunk: HTTP {resp.status_code} {resp.text[:200]}",
                            status_code=resp.status_code,
                        )
                else:
                    # Intermediate chunk — GCS returns 308 Resume Incomplete.
                    if resp.status_code != 308:
                        raise UploadError(
                            f"GCS rejected chunk: HTTP {resp.status_code} {resp.text[:200]}",
                            status_code=resp.status_code,
                        )
                pos += len(chunk)
                if progress is not None:
                    progress(pos, size)
