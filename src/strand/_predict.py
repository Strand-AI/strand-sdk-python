"""Predict namespace: estimate + submit."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from ._models import Estimate

if TYPE_CHECKING:
    from ._client import Client
    from ._http import HttpSession
    from ._jobs import Job


def _coerce_markers(markers: Iterable[str]) -> list[str]:
    out = [m for m in (s.strip() for s in markers) if m]
    if not out:
        raise ValueError("markers must contain at least one non-empty entry.")
    return out


class Predict:
    """Public predict namespace exposed on `Client.predict`."""

    def __init__(self, http: HttpSession, client: Client) -> None:
        self._http = http
        self._client = client

    def estimate(self, upload_id: str, markers: Sequence[str]) -> Estimate:
        """Compute credits required for `(upload_id, markers)`. No reservation."""
        body = {"uploadId": upload_id, "markers": _coerce_markers(markers)}
        raw = self._http.request_json("POST", "/predict/estimate", json=body)
        return Estimate._from_dict(raw)

    def submit(self, upload_id: str, markers: Sequence[str]) -> Job:
        """Submit a job. Atomically reserves credits. Returns a `Job` immediately.

        Raises:
            InsufficientCreditsError: 402 — not enough credits to reserve.
            RateLimitError: 429 — per-org concurrent job cap exceeded.
            NotFoundError: 404 — upload not found in the calling org.
        """
        from ._jobs import Job

        body = {"uploadId": upload_id, "markers": _coerce_markers(markers)}
        raw = self._http.request_json("POST", "/predict", json=body, expected=(202,))
        return Job(
            id=str(raw["jobId"]),
            reserved_credits=int(raw.get("reservedCredits", 0)),
            client=self._client,
        )
