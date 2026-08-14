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
    # Resolved auto-segmentation decision for this upload (per-upload override,
    # else org default, else true). None for freshly-initiated sessions until
    # the row is fetched via list/get.
    auto_segment: bool | None = None

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
            auto_segment=raw["autoSegment"] if isinstance(raw.get("autoSegment"), bool) else None,
            created_at=_parse_dt(raw.get("createdAt")),
        )

    def _with_completion(self, raw: dict[str, Any]) -> Upload:
        """Fold a POST /uploads/{id}/complete response into this Upload.

        Slide dimensions are *not* part of this response. Completion now only
        hands the sample to de-identification — the WSI is still in the
        quarantine bucket, and level-0 dimensions are read later, off the
        de-identified copy, before the sample reaches `ready`. So `widthPx` /
        `heightPx` are absent here and stay None until a subsequent
        `uploads.get(...)`.

        Everything is read defensively: a completion that raced ahead of us
        (`skipped: true`) echoes only `uploadId` and `status`.
        """
        return Upload(
            id=self.id,
            upload_url=self.upload_url,
            gcs_path=self.gcs_path,
            filename=self.filename,
            file_size=self.file_size,
            width_px=int(raw["widthPx"]) if isinstance(raw.get("widthPx"), int) else None,
            height_px=int(raw["heightPx"]) if isinstance(raw.get("heightPx"), int) else None,
            status=str(raw["status"]) if raw.get("status") is not None else None,
            auto_segment=self.auto_segment,
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
class Sample:
    """A sample's curated read model, from `client.samples.get(sample_id)`.

    Expiration is a field group on the sample — `will_expire` is `False` only
    when the sample never expires (a `custom` pin, or an org with no default
    policy), in which case `expires_at` and `expires_in_days` are `None`.

    Attributes:
        id: Sample UUID.
        name: Human-friendly display name, or `None` if unset.
        filename: Original uploaded filename.
        status: Lifecycle status (`uploading`, `preprocessing`, `ready`,
            `preprocess_failed`).
        file_size: Uploaded file size in bytes, or `None` if unparseable.
        width_px: Level-0 width in pixels, or `None` before the dimensions
            probe completes.
        height_px: Level-0 height in pixels, or `None`.
        mpp: Effective microns per pixel (a single isotropic value), or `None`
            when the sample has no usable scale yet.
        tags: The sample's canonical tags, sorted alphabetically.
        created_at: When the sample was created.
        expires_at: When the sample moves to Trash, or `None` if it never
            expires.
        expires_at_source: `"org_default"`, `"custom"`, or `None`.
        expires_in_days: Whole days until expiry, clamped at 0 for a sample
            at/past its date but not yet swept. `None` when it never expires.
        will_expire: True when the sample has an expiration date set.
        trashed_at: When the sample entered Trash, or `None` if still active.
            Trashed samples are permanently deleted 7 days after this time.
    """

    id: str
    name: str | None
    filename: str
    status: str
    file_size: int | None
    width_px: int | None
    height_px: int | None
    mpp: float | None
    tags: list[str]
    created_at: datetime | None
    expires_at: datetime | None
    expires_at_source: Literal["org_default", "custom"] | None
    expires_in_days: int | None
    will_expire: bool
    trashed_at: datetime | None

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> Sample:
        size_raw = raw.get("fileSize")
        try:
            file_size = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            file_size = None
        mpp_raw = raw.get("mpp")
        mpp = float(mpp_raw) if isinstance(mpp_raw, (int, float)) else None
        days_raw = raw.get("expiresInDays")
        return cls(
            id=str(raw["id"]),
            name=raw.get("name"),
            filename=str(raw["filename"]),
            status=str(raw["status"]),
            file_size=file_size,
            width_px=raw["widthPx"] if isinstance(raw.get("widthPx"), int) else None,
            height_px=raw["heightPx"] if isinstance(raw.get("heightPx"), int) else None,
            mpp=mpp,
            tags=[str(t) for t in raw.get("tags", [])],
            created_at=_parse_dt(raw.get("createdAt")),
            expires_at=_parse_dt(raw.get("expiresAt")),
            expires_at_source=raw.get("expiresAtSource"),
            expires_in_days=int(days_raw) if isinstance(days_raw, int) else None,
            will_expire=bool(raw.get("willExpire")),
            trashed_at=_parse_dt(raw.get("trashedAt")),
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
        return self.status in {"completed", "partial_failed", "failed", "cancelled"}


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
class ResultArchiveExport:
    """Point-in-time status for a whole-result OME-Zarr ZIP export."""

    status: Literal["pending", "running", "ready", "failed"]
    format: Literal["ome-zarr-zip"]
    size_bytes: int | None
    download_url: str | None
    download_url_expires_at: datetime | None
    error: str | None
    updated_at: datetime | None

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> ResultArchiveExport:
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
class UploadCompletion:
    """Result of `client.uploads.complete(upload_id)` — the finalize step that
    hands a resumable-session upload to de-identification + preprocessing.

    `skipped` is True when the sample had already left the `uploading` state
    (a concurrent or repeated finalize), which makes the call idempotent.
    """

    upload_id: str
    status: str | None
    skipped: bool
    warning: str | None

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> UploadCompletion:
        return cls(
            upload_id=str(raw["uploadId"]),
            status=str(raw["status"]) if raw.get("status") is not None else None,
            skipped=bool(raw.get("skipped")),
            warning=str(raw["warning"]) if raw.get("warning") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class PublicSampleSummary:
    """A card in the public-cohort listing (`client.public.list()`).

    Attributes:
        public_id: Stable public id — pass to `client.public.get(...)`.
        title: Display title.
        thumbnail_url: API-relative path to the JPEG thumbnail byte endpoint.
        tags: Public display tags (cohort/site labels), sorted.
        metadata: Public-visible key/value metadata curated for the sample.
    """

    public_id: str
    title: str
    thumbnail_url: str
    tags: list[str]
    metadata: dict[str, Any]

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> PublicSampleSummary:
        return cls(
            public_id=str(raw["publicId"]),
            title=str(raw.get("title", "")),
            thumbnail_url=str(raw.get("thumbnailUrl", "")),
            tags=[str(t) for t in raw.get("tags", [])],
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class PublicSampleList:
    """One page of the curated public cohort (`client.public.list()`)."""

    items: list[PublicSampleSummary]
    page: int
    page_size: int
    total_count: int
    total_pages: int

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> PublicSampleList:
        return cls(
            items=[PublicSampleSummary._from_dict(i) for i in raw.get("items", [])],
            page=int(raw.get("page", 1)),
            page_size=int(raw.get("pageSize", 0)),
            total_count=int(raw.get("totalCount", 0)),
            total_pages=int(raw.get("totalPages", 0)),
        )


@dataclass(frozen=True, slots=True)
class PublicSampleGeometry:
    """Level-0 dimensions and microns-per-pixel of a public sample's H&E image."""

    width_px: int | None
    height_px: int | None
    mpp_x: float | None
    mpp_y: float | None

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> PublicSampleGeometry:
        def _num(key: str) -> float | None:
            v = raw.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        w = raw.get("widthPx")
        h = raw.get("heightPx")
        return cls(
            width_px=int(w) if isinstance(w, int) else None,
            height_px=int(h) if isinstance(h, int) else None,
            mpp_x=_num("mppX"),
            mpp_y=_num("mppY"),
        )


@dataclass(frozen=True, slots=True)
class PredictResult:
    """Outcome of a one-shot `client.predict(...)` call.

    Attributes:
        job_id: Backend job identifier.
        status: Terminal job status — `"completed"` (every requested marker
            delivered) or `"partial_failed"` (some markers delivered, the rest
            terminally failed after server-side retries). Total failures raise
            `JobFailedError` before this is built.
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
