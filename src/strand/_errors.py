"""Typed exceptions for the Strand SDK.

All HTTP-level failures raised by the public surface inherit from `StrandError`.
Network-level failures (`httpx.HTTPError` and friends) pass through unchanged so
callers can apply their own retry logic — we only wrap responses that the
platform itself returned with a documented error shape.
"""

from __future__ import annotations

from typing import Any


class StrandError(Exception):
    """Base class for SDK errors raised against documented API responses."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.body = body or {}


class AuthError(StrandError):
    """401 — missing / invalid / expired API key."""


class BadRequestError(StrandError):
    """400 — request body or arguments rejected by the server."""


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


class RateLimitError(StrandError):
    """429 — per-org concurrent job cap exceeded. `retry_after` is in seconds."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status_code=429, error_code="rate_limited", body=body)
        self.retry_after = retry_after


class JobFailedError(StrandError):
    """Raised by `Job.wait()` when the job terminates with `status == "failed"`."""

    def __init__(self, message: str, *, job_id: str) -> None:
        super().__init__(message, error_code="job_failed")
        self.job_id = job_id


class JobTimeoutError(StrandError):
    """Raised by `Job.wait(timeout=...)` when the wait deadline elapses before terminal status."""


class UploadError(StrandError):
    """Raised when the resumable upload session aborts or returns an unexpected response."""
