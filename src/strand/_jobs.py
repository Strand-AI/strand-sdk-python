"""Job handle: status polling, SSE waits, results download."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from httpx_sse import EventSource, SSEError

from ._errors import JobFailedError, JobTimeoutError, StrandError
from ._models import JobStatus, OmeTiffExport, ResultArchiveExport
from ._results import JobResults

if TYPE_CHECKING:
    from ._client import Client

# `partial_failed` is terminal WITH results: the run delivered some markers and
# terminally failed the rest (after server-side retries were exhausted). It does
# NOT raise JobFailedError — inspect `JobStatus.status` and the per-marker
# coverage to see what landed. `completed` always means every requested marker
# delivered.
TERMINAL_STATUSES = frozenset({"completed", "partial_failed", "failed", "cancelled"})


@dataclass
class JobEvent:
    """A single status snapshot pushed over SSE."""

    id: str | None
    status: str | None
    progress: float | None
    result_gcs_path: str | None
    raw: dict[str, Any]

    @classmethod
    def _from_payload(cls, payload: dict[str, Any]) -> JobEvent:
        return cls(
            id=payload.get("id"),
            status=payload.get("status"),
            progress=(
                float(payload["progress"])
                if isinstance(payload.get("progress"), (int, float))
                else None
            ),
            result_gcs_path=payload.get("resultGcsPath"),
            raw=payload,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class Job:
    """Handle for a submitted prediction job.

    Created by `Client.predict.submit(...)` and `Client.jobs.get(...)`.
    """

    def __init__(self, *, id: str, reserved_credits: int | None, client: Client) -> None:
        self.id = id
        self.reserved_credits = reserved_credits
        self._client = client
        self._http = client._http
        self._cached_status: JobStatus | None = None

    # ---------- public surface ----------

    def __repr__(self) -> str:
        s = self._cached_status.status if self._cached_status else "unknown"
        return f"Job(id={self.id!r}, status={s!r})"

    def refresh(self) -> JobStatus:
        """Fetch the latest status snapshot and cache it on the job."""
        raw = self._http.request_json("GET", f"/jobs/{self.id}")
        status = JobStatus._from_dict(raw)
        self._cached_status = status
        return status

    def cancel(self) -> JobStatus:
        """Request cancellation of an in-flight job.

        Atomically flips the server-side status to ``cancelled`` and refunds
        the credit reservation. Markers already written before cancel are
        preserved on the sample; the GPU side is not interrupted.

        Returns:
            The post-cancel :class:`JobStatus` snapshot (status will be
            ``"cancelled"``).

        Raises:
            BadRequestError: the job is already in a terminal status.
            NotFoundError:   the job doesn't exist or belongs to another org.
        """
        self._http.request_json("POST", f"/jobs/{self.id}/cancel")
        return self.refresh()

    @property
    def status(self) -> JobStatus:
        """Most recently fetched status. Calls `refresh()` if none cached."""
        if self._cached_status is None:
            return self.refresh()
        return self._cached_status

    def stream_events(self) -> Iterator[JobEvent]:
        """Yield `JobEvent`s as the server emits them.

        The generator closes when the job reaches a terminal status. The platform
        emits `: keep-alive` heartbeats; httpx-sse filters those out.
        """
        resp = self._http.stream_response("GET", f"/jobs/{self.id}/stream")
        try:
            try:
                event_source = EventSource(resp)
                for sse in event_source.iter_sse():
                    if sse.event and sse.event != "message":
                        continue
                    data = sse.data
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if "error" in payload and "status" not in payload:
                        raise StrandError(
                            f"Server reported error in stream: {payload['error']}",
                            body=payload,
                        )
                    event = JobEvent._from_payload(payload)
                    yield event
                    if event.is_terminal:
                        return
            except SSEError as exc:  # malformed stream → fall back to polling.
                raise StrandError(f"Malformed SSE stream: {exc}") from exc
        finally:
            resp.close()

    def wait(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 2.0,
        use_stream: bool = True,
    ) -> JobStatus:
        """Block until the job reaches a terminal status.

        Args:
            timeout: Max seconds to wait. `None` waits forever.
            poll_interval: Used by the polling fallback if `use_stream=False`
                or if the stream connection drops mid-job.
            use_stream: When `True` (default), prefer SSE; fall back to polling
                if the stream errors out.

        Returns:
            The terminal `JobStatus`.

        Raises:
            JobFailedError: status terminates as `"failed"` (nothing was
                delivered). A `"partial_failed"` terminal status — some markers
                delivered, some failed — returns normally; check
                `JobStatus.status` to distinguish it from `"completed"`.
            JobTimeoutError: `timeout` elapses before terminal status.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None

        def _check_deadline() -> None:
            if deadline is not None and time.monotonic() > deadline:
                raise JobTimeoutError(
                    f"Job {self.id} did not reach terminal status within {timeout}s",
                )

        if use_stream:
            try:
                for event in self.stream_events():
                    _check_deadline()
                    if event.is_terminal:
                        break
            except (httpx.HTTPError, StrandError):
                # Drop down to polling — possibly a transient disconnect.
                pass

        while True:
            _check_deadline()
            status = self.refresh()
            if status.is_terminal:
                if status.status == "failed":
                    raise JobFailedError(
                        status.error_message or f"Job {self.id} failed",
                        job_id=self.id,
                    )
                return status
            sleep_for = poll_interval
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            if sleep_for <= 0:
                _check_deadline()
            time.sleep(sleep_for)

    def results(self) -> JobResults:
        """Return a `JobResults` handle (lazy — does not fetch zarr bytes)."""
        raw = self._http.request_json("GET", f"/jobs/{self.id}/results")
        return JobResults(
            job_id=self.id,
            result_url=str(raw["resultUrl"]),
            result_base_path=str(raw.get("resultBasePath", "")),
            expires_at=str(raw["expiresAt"]),
            client=self._client,
        )

    def download_results(
        self,
        path: str | None = None,
    ) -> Any:
        """Download all result zarr files.

        - If `path` is `None` (default), parse the zarr in-memory and return an
          `AnnData` object (requires the `anndata` extra).
        - If `path` is given, write the zarr store to that directory and return
          the `Path`. No `anndata` dependency required.
        """
        results = self.results()
        if path is None:
            return results.to_anndata()
        return results.download_to(path)

    def request_ome_tiff_export(self) -> OmeTiffExport:
        """Start or reuse an asynchronous OME-TIFF export.

        The request is idempotent. A completed job starts rendering on the
        first call; later calls return the in-progress or cached export.
        """
        raw = self._http.request_json(
            "POST",
            f"/jobs/{self.id}/exports/ome-tiff",
            expected=(200, 202),
        )
        return OmeTiffExport._from_dict(raw)

    def get_ome_tiff_export(self) -> OmeTiffExport:
        """Fetch the current OME-TIFF export status and signed URL, if ready."""
        raw = self._http.request_json(
            "GET",
            f"/jobs/{self.id}/exports/ome-tiff",
            expected=(200, 202),
        )
        return OmeTiffExport._from_dict(raw)

    def request_results_archive(self) -> ResultArchiveExport:
        """Start or reuse a cached whole-result OME-Zarr ZIP export."""
        raw = self._http.request_json(
            "POST",
            f"/jobs/{self.id}/exports/ome-zarr-zip",
            expected=(200, 202),
        )
        return ResultArchiveExport._from_dict(raw)

    def get_results_archive(self) -> ResultArchiveExport:
        """Fetch result-archive status and its signed URL when ready."""
        raw = self._http.request_json(
            "GET",
            f"/jobs/{self.id}/exports/ome-zarr-zip",
            expected=(200, 202),
        )
        return ResultArchiveExport._from_dict(raw)

    def export_ome_tiff(
        self,
        path: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 2.0,
    ) -> Path:
        """Request, wait for, and download this job's OME-TIFF result.

        Args:
            path: Destination file path. Parent directories are created.
            timeout: Maximum seconds to wait. ``None`` waits forever.
            poll_interval: Seconds between export-status requests.

        Returns:
            The destination :class:`Path`.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        export = self.request_ome_tiff_export()
        while export.status != "ready":
            if deadline is not None and time.monotonic() >= deadline:
                raise JobTimeoutError(
                    f"OME-TIFF export for job {self.id} was not ready within {timeout}s",
                )
            sleep_for = poll_interval
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)
            export = self.get_ome_tiff_export()

        if export.download_url is None:
            raise StrandError("Ready OME-TIFF export did not include a download URL")

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        download_timeout = httpx.Timeout(connect=60.0, read=None, write=60.0, pool=60.0)
        with httpx.stream(
            "GET",
            export.download_url,
            follow_redirects=True,
            timeout=download_timeout,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        return destination
