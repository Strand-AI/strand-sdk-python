"""Samples namespace — per-sample retention overrides + restore.

Exposed as `client.samples` (Phase 2). Mirrors the REST endpoints:

    PATCH /api/v1/samples/{id}/retention
    PATCH /api/v1/samples/retention          (bulk)
    POST  /api/v1/samples/{id}/restore

The retention modes are mutually exclusive — the SDK validates this client-
side so misuse raises a clear `BadRequestError` before the round-trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._http import HttpSession


def _format_expires_at(value: datetime | str | None) -> str | None:
    """Normalize to ISO 8601 UTC. Pass-through for already-stringified values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        # Naive datetime — treat as UTC. We surface this convention in the
        # docstrings; coercing here avoids the surprising "off by hours"
        # result you'd otherwise get from .isoformat() with a naive value.
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _build_payload(
    *,
    expires_at: datetime | str | None,
    pin: bool,
    use_org_default: bool,
    reason: str | None,
) -> dict[str, Any]:
    # Mutually exclusive: exactly one of the three modes.
    if sum([expires_at is not None, pin, use_org_default]) != 1:
        raise ValueError(
            "Provide exactly one of: expires_at=<datetime|ISO str>, pin=True, or use_org_default=True"
        )
    body: dict[str, Any] = {}
    if pin:
        body["pin"] = True
    elif use_org_default:
        body["useOrgDefault"] = True
    else:
        body["expiresAt"] = _format_expires_at(expires_at)
    if reason is not None:
        body["reason"] = reason
    return body


class Samples:
    """`client.samples` namespace (Phase 2)."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def set_retention(
        self,
        sample_id: str,
        expires_at: datetime | str | None = None,
        *,
        pin: bool = False,
        use_org_default: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Set retention on a single sample.

        Exactly one of `expires_at`, `pin=True`, or `use_org_default=True`.
        `pin=True` is equivalent to passing `expires_at=None` with the
        override semantics — the sample is anchored against future org
        policy changes. A naive datetime is treated as UTC.

        Args:
            sample_id: UUID of the sample.
            expires_at: Custom expiry as a `datetime` or ISO 8601 string.
            pin: Pin indefinitely (overrides org policy).
            use_org_default: Revert to org default retention.
            reason: Optional governance reason (10-500 chars).

        Returns:
            Server's updated sample payload (`id`, `expiresAt`, `expiresAtSource`,
            `retentionChangedAt`, `retentionChangedBy`, `batchId`).
        """
        body = _build_payload(
            expires_at=expires_at,
            pin=pin,
            use_org_default=use_org_default,
            reason=reason,
        )
        return self._http.request_json("PATCH", f"/samples/{sample_id}/retention", json=body)

    def set_retention_bulk(
        self,
        sample_ids: Iterable[str],
        expires_at: datetime | str | None = None,
        *,
        pin: bool = False,
        use_org_default: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Set retention on a batch of samples (max 500).

        All-or-nothing: if any sample fails the permission gate (caller isn't
        the sample creator, an org owner/admin, or a Strand admin), no rows
        are touched.

        Returns:
            `{ "updated": N, "batchId": "<uuid>" }`.
        """
        ids = list(sample_ids)
        body = _build_payload(
            expires_at=expires_at,
            pin=pin,
            use_org_default=use_org_default,
            reason=reason,
        )
        body["sampleIds"] = ids
        return self._http.request_json("PATCH", "/samples/retention", json=body)

    def restore(self, sample_id: str) -> dict[str, Any]:
        """Restore an archived sample.

        Sets `archived_at` back to null and bumps `expires_at` to at least 30
        days from now so the nightly reaper doesn't immediately re-archive
        the sample. Caller must have the same permissions required for
        `set_retention`.
        """
        return self._http.request_json("POST", f"/samples/{sample_id}/restore")
