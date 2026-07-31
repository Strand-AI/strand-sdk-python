"""User-facing typed models.

Mirror the public OpenAPI shapes but expose snake_case fields, which Python
callers expect. The generated transport under `_generated/` keeps the wire
shapes (`uploadId`, etc.); these helpers translate at the boundary so the
ergonomic surface is fully PEP 8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ._results import JobResults


# Canonical Lattice version label echoed back by the platform on every
# job-shaped response. Strictly v0.X. Historical rows from before the
# versioning rollout may surface as `"v0.1"` (sunset; readable but not
# dispatchable) via `JobStatus.model` — that's the badge string for
# rows that ran on the legacy `wx0hp7fb` 35-marker base. The
# `PredictResult.model` returned from a *fresh* `client.predict(...)`
# is always one of the live v0.X labels.
#
# Pre-2026-06-03 this list included `"v0.3"` — design note §8.2's
# original numbering put the sunset 35-marker base there. The
# 2026-06-03 renumber collapsed the v0.1 / v0.2 gap (those checkpoints
# never served prod) by relabelling the sunset entry as `v0.1` directly.
# The historical-row backfill migration `0031_postman_v03_to_v01.sql`
# is what makes that change visible on the wire.
#
# Kept type-aliased rather than imported from `_predict.ModelId` so
# this module avoids a circular import (`_predict` imports from
# `_models`). Output labels include sunset versions that can appear on
# historical jobs, so this is intentionally broader than new-job `ModelId`.
PostmanVersionLabel = Literal["v0.1", "v0.4", "v0.5", "v0.6", "v0.7"]


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # Python <3.11 datetime.fromisoformat doesn't accept trailing "Z"; normalize.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Upload:
    """Represents either a freshly-initiated upload session or an existing
    upload row fetched via `client.uploads.list()` / `.get(id)`.

    `upload_url` is only populated for sessions returned by `upload_file`
    (i.e., from `POST /uploads`). For uploads enumerated later via list/get
    it's `None`, since the resumable session URL isn't replayable.
    `filename` / `file_size` / `created_at` are populated when the row is
    fetched from the server; for newly-initiated sessions they may be `None`
    until you cross-reference via `get(id)`.
    """

    id: str
    upload_url: str | None = None
    gcs_path: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    status: str | None = None
    filename: str | None = None
    file_size: int | None = None
    created_at: datetime | None = None

    @classmethod
    def _from_create(cls, raw: dict[str, Any]) -> Upload:
        return cls(
            id=str(raw["uploadId"]),
            upload_url=str(raw["uploadUrl"]),
            gcs_path=str(raw["gcsPath"]),
        )

    @classmethod
    def _from_row(cls, raw: dict[str, Any]) -> Upload:
        """Parse a row from GET /uploads or GET /uploads/{id}."""
        file_size_raw = raw.get("fileSize")
        try:
            file_size = int(file_size_raw) if file_size_raw is not None else None
        except (TypeError, ValueError):
            file_size = None
        return cls(
            id=str(raw["id"]),
            gcs_path=str(raw["gcsPath"]) if raw.get("gcsPath") is not None else None,
            filename=str(raw["filename"]) if raw.get("filename") is not None else None,
            file_size=file_size,
            status=str(raw["status"]) if raw.get("status") is not None else None,
            width_px=int(raw["widthPx"]) if isinstance(raw.get("widthPx"), int) else None,
            height_px=int(raw["heightPx"]) if isinstance(raw.get("heightPx"), int) else None,
            created_at=_parse_dt(raw.get("createdAt")),
        )

    def _with_completion(self, raw: dict[str, Any]) -> Upload:
        return Upload(
            id=self.id,
            upload_url=self.upload_url,
            gcs_path=self.gcs_path,
            width_px=int(raw["widthPx"]),
            height_px=int(raw["heightPx"]),
            status=str(raw["status"]),
        )


@dataclass(frozen=True, slots=True)
class Estimate:
    patch_count: int
    marker_count: int
    estimated_credits: int
    org_balance: int
    org_pending: int

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> Estimate:
        return cls(
            patch_count=int(raw["patchCount"]),
            marker_count=int(raw["markerCount"]),
            estimated_credits=int(raw["estimatedCredits"]),
            org_balance=int(raw.get("orgBalance", 0)),
            org_pending=int(raw.get("orgPending", 0)),
        )


@dataclass(frozen=True, slots=True)
class JobStatus:
    """A point-in-time snapshot of job state."""

    id: str
    status: str
    progress: float | None
    reserved_credits: int | None
    markers: list[str]
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    results_available: bool
    model: PostmanVersionLabel | None

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> JobStatus:
        markers_raw = raw.get("markers") or []
        model_raw = raw.get("model")
        # Typed as `PostmanVersionLabel | None` for the user-facing surface
        # (design note §0 hard constraint: emit only v0.X). The platform
        # normalizes legacy strings before persisting, so anything else
        # would be a server-side bug — surface it untyped rather than
        # silently dropping the value.
        return cls(
            id=str(raw["id"]),
            status=str(raw["status"]),
            progress=(
                float(raw["progress"]) if isinstance(raw.get("progress"), (int, float)) else None
            ),
            reserved_credits=(
                int(raw["reservedCredits"])
                if isinstance(raw.get("reservedCredits"), int)
                else None
            ),
            markers=[str(m) for m in markers_raw],
            created_at=_parse_dt(raw.get("createdAt")),
            started_at=_parse_dt(raw.get("startedAt")),
            completed_at=_parse_dt(raw.get("completedAt")),
            error_message=raw.get("errorMessage"),
            results_available=bool(raw.get("resultsAvailable")),
            model=str(model_raw) if model_raw is not None else None,  # type: ignore[arg-type]
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class OmeTiffExport:
    """Point-in-time status for a job's asynchronous OME-TIFF export."""

    status: Literal["pending", "running", "ready", "failed"]
    format: Literal["ome-tiff"]
    size_bytes: int | None
    download_url: str | None
    download_url_expires_at: datetime | None
    error: str | None
    updated_at: datetime | None

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> OmeTiffExport:
        size_raw = raw.get("sizeBytes")
        return cls(
            status=str(raw["status"]),  # type: ignore[arg-type]
            format=str(raw["format"]),  # type: ignore[arg-type]
            size_bytes=int(size_raw) if isinstance(size_raw, int) else None,
            download_url=(
                str(raw["downloadUrl"]) if raw.get("downloadUrl") is not None else None
            ),
            download_url_expires_at=_parse_dt(raw.get("downloadUrlExpiresAt")),
            error=str(raw["error"]) if raw.get("error") is not None else None,
            updated_at=_parse_dt(raw.get("updatedAt")),
        )


@dataclass(frozen=True, slots=True)
class PredictResult:
    """Outcome of a one-shot `client.predict(...)` call.

    Attributes:
        job_id: Backend job identifier.
        status: Terminal job status — always `"completed"` for a returned
            `PredictResult` (failures raise `JobFailedError` before this is built).
        credits_used: Credits the platform reserved for the job.
        model: Canonical Lattice version that served the request (e.g.
            `"v0.7"`). Always a v0.X label — even when the caller passed
            a legacy alias like `"v10-fullpanel-v2"` on input, the platform
            normalizes before persisting and the response echoes the
            canonical name. `None` for backwards-compatibility with older
            servers that didn't populate the field; new deploys always set
            it. See `infra/notes/postman-versioning-2026-06.md` §4.
        marker_outputs: When `output_dir` was provided, maps each predicted
            marker name to its local subdirectory under `output_dir/markers/`.
            Empty dict otherwise — call `.results.to_anndata()` or
            `.results.to_array(name=...)` to materialize in-memory tensors.
        output_dir: The directory results were written to, or `None` if not
            downloaded.
        results: The underlying `JobResults` handle; use for selective reads
            (per-marker arrays, full zarr metadata, etc.).
    """

    job_id: str
    status: str
    credits_used: int
    model: PostmanVersionLabel | None = None
    marker_outputs: dict[str, Path] = field(default_factory=dict)
    output_dir: Path | None = None
    results: JobResults | None = None
