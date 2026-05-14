"""Job handle: status polling, SSE waits, results download."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from httpx_sse import EventSource, SSEError

from ._errors import JobFailedError, JobTimeoutError, StrandError
from ._models import JobStatus
from ._results import JobResults

if TYPE_CHECKING:
    from ._client import Client

TERMINAL_STATUSES = frozenset({"completed", "failed"})


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
            JobFailedError: status terminates as `"failed"`.
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
