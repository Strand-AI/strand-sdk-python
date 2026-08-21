"""Scoped sample reads and owned-sample mutations.

Exposed as `client.samples`. The unified read surface resolves owned sample ids
and public share ids, while mutations remain restricted to owned samples.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal, cast

from ._models import Sample, SampleList, SegmentationState
from ._public import PublicSample

if TYPE_CHECKING:
    from ._http import HttpSession


SampleScope = Literal["mine", "public", "all"]
_VALID_SCOPES = frozenset({"mine", "public", "all"})


class UnsetType:
    """Type of `UNSET`, used to distinguish omitted patch fields from null."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = UnsetType()


def _format_expires_at(value: datetime | str | None) -> str | None:
    """Normalize to ISO 8601 UTC. Pass through already-stringified values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        # Naive datetime values use UTC, matching the existing SDK convention.
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
    """Scoped reads and owned-sample mutations exposed on `client.samples`."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def list(
        self,
        *,
        scope: SampleScope = "mine",
        limit: int = 48,
        cursor: str | None = None,
        tag: str | None = None,
    ) -> SampleList:
        """List owned samples, public samples, or both using cursor pagination.

        `scope` is validated locally and always sent. Pagination and tag inputs
        remain server-validated.
        """
        if not isinstance(scope, str) or scope not in _VALID_SCOPES:
            raise ValueError("scope must be exactly one of: mine, public, all")
        params: dict[str, Any] = {"scope": scope, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if tag is not None:
            params["tag"] = tag
        payload = self._http.request_json("GET", "/samples", params=params)
        return SampleList._from_dict(payload)

    def get(self, sample_id: str) -> Sample | PublicSample:
        """Fetch owned detail with job history or public detail by canonical id."""
        payload = self._http.request_json("GET", f"/samples/{sample_id}")
        ownership = payload.get("ownership")
        if ownership == "mine":
            return Sample._from_dict(payload)
        if ownership == "public":
            return PublicSample._from_dict(self._http, payload)
        raise ValueError(f"Unknown sample ownership discriminator: {ownership!r}")

    def segmentation(self, sample_id: str) -> SegmentationState:
        """Return the owned sample's cell-segmentation lifecycle state."""
        payload = self._http.request_json("GET", f"/samples/{sample_id}/segmentation")
        return SegmentationState._from_dict(payload)

    def segment(self, sample_id: str) -> SegmentationState:
        """Start or retry free cell segmentation for an owned ready sample.

        The operation is idempotent: completed or in-flight work is returned,
        failed work is retried after the server's cooldown, and public sample
        identifiers are never mutable.
        """
        payload = self._http.request_json(
            "POST", f"/samples/{sample_id}/segmentation", expected=(200, 202)
        )
        return SegmentationState._from_dict(payload)

    def segmentation_manifest(self, sample_id: str, layer_id: str) -> dict[str, Any]:
        """Return the explicit mask, cell morphology, and expression manifest."""
        return self._http.request_json(
            "GET", f"/samples/{sample_id}/segmentation/{layer_id}/manifest"
        )

    def segmentation_file(self, sample_id: str, layer_id: str, path: str) -> bytes:
        """Read one mask-Zarr, morphology, expression, or provenance file."""
        return self._http.request_bytes(
            "GET", f"/samples/{sample_id}/segmentation/{layer_id}/files/{path.lstrip('/')}"
        )

    def patch(
        self,
        sample_id: str,
        *,
        name: str | UnsetType | None = UNSET,
        tags: Sequence[str] | UnsetType = UNSET,
        mpp: float | UnsetType = UNSET,
    ) -> Sample:
        """Patch an owned sample's name, complete tag set, and/or physical scale.

        `name=None` clears the display name. `tags` has set semantics, so pass
        the complete desired tag set; `tags=[]` clears user-editable tags.
        Fields left as `UNSET` are omitted from the request.
        """
        if name is UNSET and tags is UNSET and mpp is UNSET:
            raise ValueError("Provide at least one of: name, tags, or mpp")
        body: dict[str, Any] = {}
        if name is not UNSET:
            body["name"] = name
        if tags is not UNSET:
            if isinstance(tags, str):
                raise ValueError("tags must be a sequence of tag strings, not a string")
            body["tags"] = list(cast(Sequence[str], tags))
        if mpp is not UNSET:
            body["mpp"] = _validate_mpp(cast(float, mpp), "mpp")
        payload = self._http.request_json("PATCH", f"/samples/{sample_id}", json=body)
        return Sample._from_dict(payload)

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
        `use_org_default=True`. Custom expirations survive future org-policy
        changes; `use_org_default=True` clears the custom expiration. A naive
        datetime is treated as UTC.
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
        """Set expiration on up to 500 samples as one all-or-nothing batch.

        If any sample fails the permission gate, no rows are changed.
        """
        body = _build_payload(
            expires_at=expires_at,
            never_expire=never_expire,
            use_org_default=use_org_default,
            reason=reason,
        )
        body["sampleIds"] = list(sample_ids)
        return self._http.request_json("PATCH", "/samples/expiration", json=body)

    def restore(self, sample_id: str) -> dict[str, Any]:
        """Restore a sample during its seven-day Trash window.

        The sample returns to the active list and its expiration is extended so
        it is not immediately re-trashed.
        """
        return self._http.request_json("POST", f"/samples/{sample_id}/restore")


def _validate_mpp(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number greater than 0 and at most 100")
    result = float(value)
    if not isfinite(result) or result <= 0 or result > 100:
        raise ValueError(f"{name} must be a number greater than 0 and at most 100")
    return result
