"""Typed exceptions for the Strand SDK.

All HTTP-level failures raised by the public surface inherit from `StrandError`.
Network-level failures (`httpx.HTTPError` and friends) pass through unchanged so
callers can apply their own retry logic — we only wrap responses that the
platform itself returned with a documented error shape.
"""

from __future__ import annotations

from typing import Any


class StrandError(Exception):
    """Base class for SDK errors raised against documented API responses.

    `upload_id` is set by `client.predict(...)` when an error bubbles out of
    the pipeline after the upload step succeeded. It lets callers resume the
    job without re-uploading the WSI:

        try:
            client.predict("slide.svs", markers=[...])
        except StrandError as e:
            if e.upload_id:
                job = client.predict.submit(e.upload_id, markers=[...])
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        body: dict[str, Any] | None = None,
        upload_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.body = body or {}
        self.upload_id = upload_id


class AuthError(StrandError):
    """401 — missing / invalid / expired API key."""


class BadRequestError(StrandError):
    """400 — request body or arguments rejected by the server."""


class UnknownMarkerError(BadRequestError):
    """400 — one or more marker names aren't recognized by the platform.

    Caught explicitly so callers can prompt for a fixed list without parsing
    error bodies. `unknown` is the list of names the server flagged. `known_subset`
    is a sampling of valid names when the server provides it (may be `None`).
    """

    def __init__(
        self,
        message: str,
        *,
        unknown: list[str],
        known_subset: list[str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status_code=400, error_code="unknown_markers", body=body)
        self.unknown = unknown
        self.known_subset = known_subset


class MarkerNotAvailableError(StrandError):
    """403 — one or more requested markers aren't available on this account.

    The full marker panel is an entitlement (contracted partners under
    agreement); self-signup accounts may request only the public panel.
    `unavailable` is the list of requested markers the server rejected;
    `available` is the account's allowed marker set when the server provides it
    (may be `None`). No full-panel enumeration is exposed to gated accounts.
    """

    def __init__(
        self,
        message: str,
        *,
        unavailable: list[str],
        available: list[str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status_code=403, error_code="marker_not_available", body=body)
        self.unavailable = unavailable
        self.available = available


class NotFoundError(StrandError):
    """404 — referenced resource (upload, job, file) does not exist or isn't accessible."""


class InsufficientCreditsError(StrandError):
    """402 — org has insufficient credits to reserve for this job.

    Attributes:
        required: Credits required to run the job, as returned by the server.
        balance:  Best-effort cached org balance from the most recent estimate, if available.
    """

    def __init__(
        self,
        message: str,
        *,
        required: int | None = None,
        balance: int | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status_code=402, error_code="insufficient_credits", body=body)
        self.required = required
        self.balance = balance


class JobFailedError(StrandError):
    """Raised by `Job.wait()` when the job terminates with `status == "failed"`."""

    def __init__(self, message: str, *, job_id: str) -> None:
        super().__init__(message, error_code="job_failed")
        self.job_id = job_id


class JobTimeoutError(StrandError):
    """Raised by `Job.wait(timeout=...)` when the wait deadline elapses before terminal status."""


class UploadError(StrandError):
    """Raised when the resumable upload session aborts or returns an unexpected response."""
