"""Samples namespace — physical scale, expiration overrides, and restore.

Exposed as `client.samples` (Phase 2). Mirrors the REST endpoints:

    PATCH /api/v1/samples/{id}/expiration
    PATCH /api/v1/samples/expiration         (bulk)
    PATCH /api/v1/samples/{id}/mpp
    POST  /api/v1/samples/{id}/restore

The expiration modes are mutually exclusive — the SDK validates this client-
side so misuse raises a clear `ValueError` before the round-trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from math import isfinite
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
    never_expire: bool,
    use_org_default: bool,
    reason: str | None,
) -> dict[str, Any]:
    # Mutually exclusive: exactly one of the three modes.
    if sum([expires_at is not None, never_expire, use_org_default]) != 1:
        raise ValueError(
            "Provide exactly one of: expires_at=<datetime|ISO str>, "
            "never_expire=True, or use_org_default=True"
        )
    body: dict[str, Any] = {}
    if never_expire:
        body["neverExpire"] = True
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

    def set_mpp(
        self,
        sample_id: str,
        mpp_x: float,
        mpp_y: float | None = None,
    ) -> dict[str, Any]:
        """Set the user-reported physical pixel size for a sample.

        The value is measured in microns per pixel at the slide's base level.
        It takes precedence over embedded slide metadata for subsequent
        inference jobs. Omit `mpp_y` for isotropic pixels.

        Args:
            sample_id: UUID of the sample or completed upload.
            mpp_x: Horizontal microns per pixel, greater than 0 and at most 100.
            mpp_y: Optional vertical microns per pixel. Defaults to `mpp_x`.

        Returns:
            Server payload with `id` and normalized `userMpp.{x,y}` values.
        """
        x = _validate_mpp(mpp_x, "mpp_x")
        if mpp_y is None:
            mpp: float | dict[str, float] = x
        else:
            mpp = {"x": x, "y": _validate_mpp(mpp_y, "mpp_y")}
        return self._http.request_json("PATCH", f"/samples/{sample_id}/mpp", json={"mpp": mpp})

    def set_expiration(
        self,
        sample_id: str,
        expires_at: datetime | str | None = None,
        *,
        never_expire: bool = False,
        use_org_default: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Set expiration on a single sample.

        Exactly one of `expires_at`, `never_expire=True`, or
        `use_org_default=True`. Custom expirations (date or never) survive
        future org-policy changes; `use_org_default=True` clears the custom
        expiration. A naive datetime is treated as UTC.

        Args:
            sample_id: UUID of the sample.
            expires_at: Custom expiration as a `datetime` or ISO 8601 string.
            never_expire: Set the sample to never expire (custom).
            use_org_default: Clear custom expiration; follow org policy.
            reason: Optional governance reason (10-500 chars).

        Returns:
            Server's updated sample payload (`id`, `expiresAt`, `expiresAtSource`,
            `retentionChangedAt`, `retentionChangedBy`, `batchId`).
        """
        body = _build_payload(
            expires_at=expires_at,
            never_expire=never_expire,
            use_org_default=use_org_default,
            reason=reason,
        )
        return self._http.request_json("PATCH", f"/samples/{sample_id}/expiration", json=body)

    def set_expiration_bulk(
        self,
        sample_ids: Iterable[str],
        expires_at: datetime | str | None = None,
        *,
        never_expire: bool = False,
        use_org_default: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Set expiration on a batch of samples (max 500).

        All-or-nothing: if any sample fails the permission gate (caller isn't
        the sample creator, an org owner/admin, or a Strand admin), no rows
        are touched.

        Returns:
            `{ "updated": N, "batchId": "<uuid>" }`.
        """
        ids = list(sample_ids)
        body = _build_payload(
            expires_at=expires_at,
            never_expire=never_expire,
            use_org_default=use_org_default,
            reason=reason,
        )
        body["sampleIds"] = ids
        return self._http.request_json("PATCH", "/samples/expiration", json=body)

    def restore(self, sample_id: str) -> dict[str, Any]:
        """Restore a sample from Trash.

        Available within the 7-day Trash window. Brings the sample back to
        the active list and extends its expiration so it isn't immediately
        re-trashed. Caller must have the same permissions required for
        `set_expiration`.
        """
        return self._http.request_json("POST", f"/samples/{sample_id}/restore")


def _validate_mpp(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number greater than 0 and at most 100")
    result = float(value)
    if not isfinite(result) or result <= 0 or result > 100:
        raise ValueError(f"{name} must be a number greater than 0 and at most 100")
    return result
