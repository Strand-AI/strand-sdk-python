"""Predict namespace: estimate + submit, plus the one-shot pipeline."""

from __future__ import annotations

import os
import warnings
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
# (today: `"v0.5"`).
#
# v0.3 is intentionally absent — it was sunset in the versioning rollout
# (design note §8.3). The legacy `"v10"` alias resolved to v0.3, so
# passing `model="v10"` now raises a `DeprecationWarning` *and* the
# platform 400s with `unknown_model`. The other two legacy aliases
# (`"v10-fullpanel"` → `"v0.4"`, `"v10-fullpanel-v2"` → `"v0.5"`) still
# resolve and dispatch, with a `DeprecationWarning`.
ModelId = Literal["v0.4", "v0.5"]

# Legacy → canonical alias table. Kept in lockstep with the platform's
# `LEGACY_MODEL_ALIASES` map in `platform/src/lib/inference/postman-versions.ts`
# (the SDK can't import from the platform's TS source). Update both sides
# together; any drift surfaces as a 400 `unknown_model` from the server
# even though the SDK accepted the alias.
#
# Six-month sunset window per design note §4: legacy strings remain
# accepted on the wire until 2026-12-01, with `DeprecationWarning` on
# each call. After that, both the SDK and the REST layer drop them.
_LEGACY_MODEL_ALIASES: dict[str, ModelId] = {
    # "v10" resolves to v0.3 which is sunset; we still emit the warning
    # so callers see the deprecation message, but the platform will
    # return 400 unknown_model. Forwarding `"v10"` (rather than rewriting
    # to `"v0.3"`) lets the server emit a single coherent error code.
    "v10-fullpanel": "v0.4",
    "v10-fullpanel-v2": "v0.5",
}

# Sunset aliases — accepted on the input surface so existing callers
# don't `ValueError` client-side, but `_normalize_model_id` returns
# them unchanged so the server can answer with its canonical
# `unknown_model` response. Kept separate from the resolving table
# above so future readers don't accidentally route a sunset version.
_SUNSET_MODEL_ALIASES: frozenset[str] = frozenset({"v10"})


def _normalize_model_id(model: str | None) -> str | None:
    """Normalize a caller-supplied model id to its canonical v0.X label.

    - Canonical ids (`"v0.4"`, `"v0.5"`) pass through unchanged.
    - Legacy aliases (`"v10-fullpanel"`, `"v10-fullpanel-v2"`) resolve to
      their v0.X target and a `DeprecationWarning` is emitted.
    - Sunset aliases (`"v10"`) emit a `DeprecationWarning` and are
      returned unchanged — the server answers with 400 `unknown_model`.
    - Anything else (unknown strings) is passed through; the server is
      the authority on whether the id resolves, so the SDK does not
      client-side-validate. This keeps the SDK forward-compatible with
      new versions added on the server without a SDK release.

    Returns `None` when `model` is `None`.
    """
    if model is None:
        return None
    if model in _LEGACY_MODEL_ALIASES:
        canonical = _LEGACY_MODEL_ALIASES[model]
        warnings.warn(
            f"model={model!r} is a deprecated alias for {canonical!r}; "
            f"pass model={canonical!r} instead. Legacy aliases will be "
            f"removed in a future release (target: 2026-12-01).",
            DeprecationWarning,
            stacklevel=3,
        )
        return canonical
    if model in _SUNSET_MODEL_ALIASES:
        warnings.warn(
            f"model={model!r} refers to a sunset POSTMAN version and is no "
            f"longer dispatched. The server will reject this request with "
            f"`unknown_model`; use 'v0.4' or 'v0.5' instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        return model
    return model


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
            model: Optional explicit POSTMAN version. One of `"v0.4"` (192-marker
                original) or `"v0.5"` (192-marker retrained, current default).
                Both share GenePT embeddings, so the marker vocabulary is
                identical between them — picking a version is a model-weights
                swap, not a vocab swap. When omitted, the platform picks the
                current default (`"v0.5"`).

                Legacy aliases (`"v10-fullpanel"` → `"v0.4"`,
                `"v10-fullpanel-v2"` → `"v0.5"`) are still accepted on input
                with a `DeprecationWarning`. The `"v10"` alias (which used to
                resolve to the now-sunset v0.3) raises a `DeprecationWarning`
                and the server rejects the request with `unknown_model`.

        Raises:
            InsufficientCreditsError: 402 — not enough credits to reserve.
            RateLimitError: 429 — per-org concurrent job cap exceeded.
            NotFoundError: 404 — upload not found in the calling org.
            BadRequestError: 400 — unknown model id (or other validation).
        """
        from ._jobs import Job

        body: dict[str, object] = {
            "uploadId": upload_id,
            "markers": _coerce_markers(markers),
        }
        resolved_model = _normalize_model_id(model)
        if resolved_model is not None:
            body["model"] = resolved_model
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
    ) -> PredictResult | Job:
        """Run the full prediction pipeline in one call.

        Orchestrates: upload → submit → (optional) wait → (optional) download.
        All sub-operations use the same primitives exposed on the client, so
        callers can drop down a level whenever they need finer control.

        Args:
            image_path: Local WSI file to upload (SVS / TIFF / NDPI / …).
            markers: Markers to predict (e.g., `["HER2", "CD8", "PD1"]`).
            model: Optional explicit model id. See `predict.submit(...)`.
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
            InsufficientCreditsError, RateLimitError, NotFoundError: Per-step
                from the underlying primitives.

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
            local_path, progress=_upload_progress, if_not_exists=True
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
