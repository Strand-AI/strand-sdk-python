"""Predict namespace: estimate + submit, plus the one-shot pipeline."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from ._errors import StrandError
from ._models import Estimate, PredictResult

if TYPE_CHECKING:
    from ._client import Client
    from ._http import HttpSession
    from ._jobs import Job


ProgressCb = Callable[[str, float], None]

# Canonical SDK-routable model ids. Mirrors the platform's
# `POSTMAN_VERSIONS` map (see `infra/notes/postman-versioning-2026-06.md`
# §2 + §4). Omit `model` to let the platform pick the current default
# (today: `"v0.7"`).
#
# Sunset versions (v0.1, formerly the `"v10"` 35-marker base) are not
# selectable — the server returns 400 `unknown_model`. Legacy aliases
# (`"v10"`, `"v10-fullpanel"`, `"v10-fullpanel-v2"`) were dropped on
# 2026-06-03 (design note §4, rewritten): the platform no longer accepts
# them on input, and the SDK no longer rewrites them client-side. The
# `Literal` is intentionally NOT a strict-enum type-guard at runtime —
# callers can still pass any `str` (forward-compat with new server
# versions cut without a SDK release); the server is the authority.
ModelId = Literal["v0.4", "v0.5", "v0.7"]


def _coerce_markers(markers: Iterable[str]) -> list[str]:
    out = [m for m in (s.strip() for s in markers) if m]
    if not out:
        raise ValueError("markers must contain at least one non-empty entry.")
    return out


class Predict:
    """Public predict namespace exposed on `Client.predict`.

    The instance is **callable**: `client.predict(image_path, markers=[...])`
    runs the full pipeline (upload → submit → wait → download) and blocks
    until completion. The lower-level primitives (`estimate`, `submit`) stay
    available as namespace methods.
    """

    def __init__(self, http: HttpSession, client: Client) -> None:
        self._http = http
        self._client = client

    def estimate(self, upload_id: str, markers: Sequence[str]) -> Estimate:
        """Compute credits required for `(upload_id, markers)`. No reservation."""
        body = {"uploadId": upload_id, "markers": _coerce_markers(markers)}
        raw = self._http.request_json("POST", "/predict/estimate", json=body)
        return Estimate._from_dict(raw)

    def submit(
        self,
        upload_id: str,
        markers: Sequence[str],
        *,
        model: ModelId | str | None = None,
    ) -> Job:
        """Submit a job. Atomically reserves credits. Returns a `Job` immediately.

        Args:
            upload_id: Sample/upload identifier to run inference against.
            markers: Markers to predict.
            model: Optional explicit Lattice version. `"v0.7"` is the current
                dispatchable version and default. `"v0.4"` / `"v0.5"` remain
                typed because the public API preserves their canonical ids for
                structured `model_sunset` errors. The historical `"v0.6"` id is
                intentionally output-only and is not a valid new-job selection.
                When omitted, the platform picks the current default (`"v0.7"`).

                Legacy aliases (`"v10"`, `"v10-fullpanel"`, `"v10-fullpanel-v2"`)
                were dropped on 2026-06-03 — the server now rejects them with
                400 `unknown_model`. Pass the canonical v0.X id directly.

        Raises:
            InsufficientCreditsError: 402 — not enough credits to reserve.
            NotFoundError: 404 — upload not found in the calling org.
            BadRequestError: 400 — unknown model id (or other validation).
        """
        from ._jobs import Job

        body: dict[str, object] = {
            "uploadId": upload_id,
            "markers": _coerce_markers(markers),
        }
        if model is not None:
            body["model"] = model
        raw = self._http.request_json("POST", "/predict", json=body, expected=(202,))
        return Job(
            id=str(raw["jobId"]),
            reserved_credits=int(raw.get("reservedCredits", 0)),
            client=self._client,
        )

    # `wait=True` (default) returns a fully-resolved `PredictResult`.
    # `wait=False` returns the in-flight `Job` so callers can poll / wait
    # later. The overloads make either path type-safe without a Union return.
    @overload
    def __call__(
        self,
        image_path: str | os.PathLike[str],
        markers: Sequence[str],
        *,
        model: ModelId | str | None = ...,
        wait: Literal[True] = ...,
        timeout_sec: float = ...,
        output_dir: str | os.PathLike[str] | None = ...,
        poll_interval_sec: float = ...,
        on_progress: ProgressCb | None = ...,
        auto_segment: bool | None = ...,
        mpp: float | tuple[float, float] | None = ...,
    ) -> PredictResult: ...

    @overload
    def __call__(
        self,
        image_path: str | os.PathLike[str],
        markers: Sequence[str],
        *,
        model: ModelId | str | None = ...,
        wait: Literal[False],
        timeout_sec: float = ...,
        output_dir: str | os.PathLike[str] | None = ...,
        poll_interval_sec: float = ...,
        on_progress: ProgressCb | None = ...,
        auto_segment: bool | None = ...,
        mpp: float | tuple[float, float] | None = ...,
    ) -> Job: ...

    def __call__(
        self,
        image_path: str | os.PathLike[str],
        markers: Sequence[str],
        *,
        model: ModelId | str | None = None,
        wait: bool = True,
        timeout_sec: float = 1800.0,
        output_dir: str | os.PathLike[str] | None = None,
        poll_interval_sec: float = 5.0,
        on_progress: ProgressCb | None = None,
        auto_segment: bool | None = None,
        mpp: float | tuple[float, float] | None = None,
    ) -> PredictResult | Job:
        """Run the full prediction pipeline in one call.

        Orchestrates: upload → submit → (optional) wait → (optional) download.
        All sub-operations use the same primitives exposed on the client, so
        callers can drop down a level whenever they need finer control.

        Args:
            image_path: Local WSI file to upload (SVS / TIFF / NDPI / …).
            markers: Markers to predict (e.g., `["HER2", "CD8", "PD1"]`).
            model: Optional explicit model id. See `predict.submit(...)`.
            auto_segment: Opt out of automatic cell segmentation for the uploaded
                slide. `None` (default) uses the org default; `False` skips
                segmentation; `True` forces it on. Ignored on a dedup hit against
                an already-uploaded slide (the earlier decision stands).
            mpp: User-reported microns per pixel for the uploaded slide, when the
                caller already knows its scale. Persisted at creation and wins
                over the slide's own calibrated value — no separate
                `samples.set_mpp(...)` call needed. Isotropic: a float, or an
                `(x, y)` tuple with equal axes; > 0 and <= 100. Ignored on a
                dedup hit (the existing sample's scale stands).
            wait: When `True` (default), block through upload → submit → wait
                → download and return a `PredictResult`. When `False`, return
                a `Job` handle once the upload + submit complete — caller
                drives `.wait()` / `.download_results()` later. `timeout_sec`,
                `poll_interval_sec`, `output_dir`, and the `"wait"` /
                `"download"` progress stages are ignored when `wait=False`.
            timeout_sec: Max seconds to wait for the job to finish.
            output_dir: If provided, mirror the full zarr result store under
                this directory. When `None`, no files are written — use
                `result.results.to_anndata()` / `to_array(...)` to materialize.
            poll_interval_sec: Status-poll cadence when SSE drops out.
            on_progress: Optional `(stage, fraction)` callback. `stage` is
                one of `"upload"`, `"submit"`, `"wait"`, `"download"` (only
                `"upload"` + `"submit"` fire when `wait=False`).
                `fraction` is always a float in `[0.0, 1.0]` — `0.0` at the
                start of each stage and `1.0` at its end, with intermediate
                values where the underlying step exposes progress.

        Returns:
            `PredictResult` with `job_id`, `status="completed"`, `credits_used`,
            `marker_outputs` (paths when `output_dir` is set), and `results`
            when `wait=True`. A `Job` handle when `wait=False`.

        Raises:
            FileNotFoundError: If `image_path` doesn't exist.
            JobTimeoutError: If the job hasn't finished within `timeout_sec`.
            JobFailedError: If the job terminates in `"failed"` state.
            InsufficientCreditsError, NotFoundError: Per-step from the
                underlying primitives.

        Errors raised after the upload step succeeds carry the resulting
        `upload_id` on `StrandError.upload_id`, so callers can resume via
        `client.predict.submit(upload_id, markers=[...])` without re-uploading
        the WSI.
        """
        # Validate inputs up-front so we fail before paying for an upload.
        validated_markers = _coerce_markers(markers)
        local_path = Path(image_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"No such file: {local_path}")

        report = on_progress or (lambda _stage, _frac: None)

        report("upload", 0.0)

        def _upload_progress(done: int, total: int) -> None:
            report("upload", done / total if total else 0.0)

        # Dedup by default: hash the file and let the platform short-circuit
        # if a non-trashed sample with the same content already exists.
        # Without this, a repeat `predict(same_file, ...)` re-uploads + races
        # the still-preprocessing original on submit(). See task #95.
        upload = self._client.uploads.upload_file(
            local_path,
            progress=_upload_progress,
            if_not_exists=True,
            auto_segment=auto_segment,
            mpp=mpp,
        )
        report("upload", 1.0)

        # From here on, any StrandError gets `upload_id` attached so callers
        # can resume without paying for the re-upload.
        try:
            report("submit", 0.0)
            job = self.submit(upload.id, validated_markers, model=model)
            report("submit", 1.0)

            if not wait:
                return job

            report("wait", 0.0)
            status = job.wait(timeout=timeout_sec, poll_interval=poll_interval_sec)
            report("wait", 1.0)

            report("download", 0.0)
            results = job.results()
            marker_outputs: dict[str, Path] = {}
            out_path: Path | None = None
            if output_dir is not None:
                out_path = Path(output_dir)
                results.download_to(out_path)
                for name in results.multiscale_names(include_he=False):
                    marker_outputs[name] = out_path / "markers" / name
            report("download", 1.0)
        except StrandError as e:
            e.upload_id = upload.id
            raise

        return PredictResult(
            job_id=job.id,
            status=status.status,
            credits_used=job.reserved_credits or 0,
            # Echo the canonical v0.X label the platform persisted. Reading
            # off `status.model` (rather than `resolved_model`) covers the
            # case where the caller omitted `model=` — the server picked
            # the default and we surface what actually ran.
            model=status.model,
            marker_outputs=marker_outputs,
            output_dir=out_path,
            results=results,
        )
