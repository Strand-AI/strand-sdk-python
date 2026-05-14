"""Uploads namespace.

Handles the three-step resumable flow:
  1. POST /api/v1/uploads          → create session
  2. PUT  <uploadUrl> w/ Content-Range  → stream chunks directly to GCS
  3. POST /api/v1/uploads/{id}/complete → finalize, read slide dims

The chunked PUT bypasses the platform; we hit GCS directly using the
resumable session URL it returns. Chunks must be a multiple of 256 KiB
per GCS spec, except the last chunk; we use 8 MiB.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ._errors import UploadError
from ._models import Upload

if TYPE_CHECKING:
    from ._http import HttpSession

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


class Uploads:
    """Public uploads namespace exposed on `Client.uploads`."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def upload_file(
        self,
        path: str | os.PathLike[str],
        *,
        content_type: str | None = None,
        chunk_size: int = CHUNK_SIZE,
        progress: ProgressCb | None = None,
    ) -> Upload:
        """Upload a local WSI file end-to-end.

        Streams in 8 MiB chunks via the GCS resumable session URL returned by
        `POST /api/v1/uploads`, then finalizes via `POST /uploads/{id}/complete`.
        The returned `Upload` carries slide dimensions (`width_px`, `height_px`)
        and `status="ready"`.

        Args:
            path: Local file to upload.
            content_type: Override the auto-detected MIME type.
            chunk_size: Bytes per PUT request. Must be a positive multiple of 256 KiB
                except for the last chunk. Defaults to 8 MiB.
            progress: Optional `(bytes_uploaded, total_bytes)` callback.

        Returns:
            `Upload` with `width_px` / `height_px` / `status="ready"` populated.

        Raises:
            FileNotFoundError: If `path` does not exist.
            UploadError: If the GCS resumable session returns an unexpected status.
        """
        local = Path(path)
        if not local.is_file():
            raise FileNotFoundError(f"No such file: {local}")
        if chunk_size <= 0 or chunk_size % (256 * 1024) != 0:
            raise ValueError(
                "chunk_size must be a positive multiple of 256 KiB (262144 bytes)."
            )

        size = local.stat().st_size
        ct = content_type or _CONTENT_TYPE_BY_EXT.get(local.suffix.lower(), DEFAULT_CONTENT_TYPE)

        session = self._initiate(local.name, size, ct)
        self._stream_to_gcs(
            session.upload_url, local, size, ct, chunk_size=chunk_size, progress=progress
        )
        return self._complete(session)

    # ---------- internal helpers ----------

    def _initiate(self, filename: str, size: int, content_type: str) -> Upload:
        raw = self._http.request_json(
            "POST",
            "/uploads",
            json={"filename": filename, "fileSize": size, "contentType": content_type},
        )
        return Upload._from_create(raw)

    def _complete(self, session: Upload) -> Upload:
        raw = self._http.request_json("POST", f"/uploads/{session.id}/complete")
        return session._with_completion(raw)

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
            httpx.Client(timeout=httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0)) as gcs,
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
