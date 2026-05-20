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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._results import JobResults


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
    """Resumable upload session."""

    id: str
    upload_url: str
    gcs_path: str
    width_px: int | None = None
    height_px: int | None = None
    status: str | None = None

    @classmethod
    def _from_create(cls, raw: dict[str, Any]) -> Upload:
        return cls(
            id=str(raw["uploadId"]),
            upload_url=str(raw["uploadUrl"]),
            gcs_path=str(raw["gcsPath"]),
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

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> JobStatus:
        markers_raw = raw.get("markers") or []
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
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed"}


@dataclass(frozen=True, slots=True)
class PredictResult:
    """Outcome of a one-shot `client.predict(...)` call.

    Attributes:
        job_id: Backend job identifier.
        status: Terminal job status — always `"completed"` for a returned
            `PredictResult` (failures raise `JobFailedError` before this is built).
        credits_used: Credits the platform reserved for the job.
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
    marker_outputs: dict[str, Path] = field(default_factory=dict)
    output_dir: Path | None = None
    results: JobResults | None = None
