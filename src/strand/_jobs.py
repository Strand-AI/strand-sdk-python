"""Job handle: status polling, SSE waits, results download."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx

from ._errors import JobFailedError, JobTimeoutError, StrandError
from ._models import JobStatus, ResultExport
from ._results import JobResults

if TYPE_CHECKING:
    from ._client import Client

# `partial_failed` is terminal WITH results: the run delivered some markers and
# terminally failed the rest (after server-side retries were exhausted). It does
# NOT raise JobFailedError — inspect `JobStatus.status` and the per-marker
# coverage to see what landed. `completed` always means every requested marker
# delivered.
TERMINAL_STATUSES = frozenset({"completed", "partial_failed", "failed", "cancelled"})


def _iter_sse_messages(
    lines: Iterator[str],
    *,
    on_activity: Callable[[], None] | None = None,
) -> Iterator[tuple[str, str]]:
    """Decode SSE fields while exposing every line, including heartbeats.

    ``httpx-sse`` intentionally hides comment heartbeats. A wait deadline must
    still be checked on those lines, so the job wait path uses this small SSE
    field decoder and calls ``on_activity`` before discarding any comment.
    """
    event = ""
    data: list[str] = []
    for raw_line in lines:
        if on_activity is not None:
            on_activity()
        line = raw_line.rstrip("\r")
        if not line:
            if data:
                yield event, "\n".join(data)
            event = ""
            data = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


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

        The generator closes when the job reaches a terminal status.
        """
        yield from self._stream_events(deadline=None, read_timeout=None)

    def _stream_events(
        self,
        *,
        deadline: float | None,
        read_timeout: float | None,
    ) -> Iterator[JobEvent]:
        """Internal stream with a wall-clock deadline for ``wait()``."""
        resp = self._http.stream_response(
            "GET",
            f"/jobs/{self.id}/stream",
            timeout=read_timeout,
        )

        def check_deadline() -> None:
            if deadline is not None and time.monotonic() >= deadline:
                raise JobTimeoutError(f"Job {self.id} did not reach terminal status in time")

        try:
            content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip()
            if content_type != "text/event-stream":
                raise StrandError(
                    f"Expected text/event-stream, got {content_type or 'no content type'}"
                )
            for event_name, data in _iter_sse_messages(
                resp.iter_lines(), on_activity=check_deadline
            ):
                if event_name and event_name != "message":
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
            _check_deadline()
            read_timeout = None
            if deadline is not None:
                read_timeout = max(0.001, deadline - time.monotonic())
            try:
                for event in self._stream_events(
                    deadline=deadline,
                    read_timeout=read_timeout,
                ):
                    _check_deadline()
                    if event.is_terminal:
                        break
            except JobTimeoutError:
                raise JobTimeoutError(
                    f"Job {self.id} did not reach terminal status within {timeout}s"
                ) from None
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

    def request_export(
        self,
        format: Literal["ome-zarr", "ome-zarr-zip", "ome-tiff"],
        *,
        include_he: bool | None = None,
        include_segmentation: bool = False,
    ) -> ResultExport:
        """Start or reuse one format-driven result export.

        Native ``ome-zarr`` is returned immediately without conversion.
        ZIP and OME-TIFF generation are asynchronous and idempotent. Set
        ``include_segmentation`` to attach the latest mask/cell-expression
        manifest; it is never available for public, read-only samples.
        """
        body: dict[str, Any] = {
            "format": format,
            "includeSegmentation": include_segmentation,
        }
        if include_he is not None:
            body["includeHe"] = include_he
        raw = self._http.request_json(
            "POST", f"/jobs/{self.id}/exports", json=body, expected=(200, 202)
        )
        return ResultExport._from_dict(raw)

    def get_export(
        self,
        format: Literal["ome-zarr", "ome-zarr-zip", "ome-tiff"],
        *,
        include_he: bool | None = None,
        include_segmentation: bool = False,
    ) -> ResultExport:
        """Fetch a format-driven export status and refreshed signed links."""
        params: dict[str, Any] = {
            "format": format,
            "includeSegmentation": str(include_segmentation).lower(),
        }
        if include_he is not None:
            params["includeHe"] = str(include_he).lower()
        raw = self._http.request_json(
            "GET", f"/jobs/{self.id}/exports", params=params, expected=(200, 202)
        )
        return ResultExport._from_dict(raw)

    def download_export(
        self,
        format: Literal["ome-zarr-zip", "ome-tiff"],
        path: str,
        *,
        include_segmentation: bool = False,
        timeout: float | None = None,
        poll_interval: float = 2.0,
    ) -> Path:
        """Request, wait for, and download one generated single-file export."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        export = self.request_export(
            format, include_he=True if format == "ome-tiff" else None,
            include_segmentation=include_segmentation,
        )
        while export.status != "ready":
            if export.status == "failed":
                raise StrandError(export.error or f"{format} export failed", error_code="export_failed")
            if deadline is not None and time.monotonic() >= deadline:
                raise JobTimeoutError(f"{format} export for job {self.id} was not ready within {timeout}s")
            sleep_for = poll_interval if deadline is None else min(
                poll_interval, max(0.0, deadline - time.monotonic())
            )
            if sleep_for > 0:
                time.sleep(sleep_for)
            export = self.get_export(
                format, include_he=True if format == "ome-tiff" else None,
                include_segmentation=include_segmentation,
            )

        prediction = export.artifacts.get("prediction") or {}
        download_url = prediction.get("downloadUrl")
        if not isinstance(download_url, str):
            raise StrandError(f"Ready {format} export did not include a download URL")
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        download_timeout = httpx.Timeout(connect=60.0, read=None, write=60.0, pool=60.0)
        with httpx.stream(
            "GET", download_url, follow_redirects=True, timeout=download_timeout
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        return destination
