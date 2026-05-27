# Changelog

All notable changes to `strand-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes on GitHub are extracted from the section header matching the
published version (e.g. `## [0.1.0]`), so keep headers in that exact form.

## [0.3.0] - 2026-05-27

### Added
- `client.predict.submit(..., model=...)` and `client.predict(..., model=...)`
  accept an optional `Literal["v10", "v10-fullpanel"]` to pick the inference
  model. Typos surface as `BadRequestError` before credits are reserved.
- `client.predict(..., wait=False)` returns a `Job` handle once upload + submit
  complete instead of blocking through the full pipeline. Overloaded so
  `wait=True` returns `PredictResult` and `wait=False` returns `Job`.
- `client.uploads.list(limit=..., cursor=...)` — cursor-paginated list of prior
  uploads (newest first), returning a `UploadList` (`uploads`, `next_cursor`).
- `client.uploads.get(upload_id)` — fetch a single upload by id.
- `UnknownMarkerError(BadRequestError)` — raised by `predict.submit` / `predict`
  when one or more marker names are not in the platform's vocabulary. Exposes
  `unknown: list[str]` and `known_subset: list[str] | None` so callers can
  recover without a re-upload.
- `StrandError.upload_id` — populated by `client.predict(...)` on any failure
  after a successful upload, so callers can re-enter the pipeline with
  `client.predict.submit(upload_id, ...)` instead of re-uploading the WSI.
- `UploadList` exported from the package surface.
- `Job.cancel()` and `client.jobs.cancel(job_id)` — request termination of an
  in-flight job. Status flips to `cancelled` (a new terminal status), the
  credit reservation is refunded, and partial outputs that already landed
  stay on the sample. Calling `cancel` on a terminal job raises
  `BadRequestError`.

### Changed
- `ProgressCb` is now `Callable[[str, float], None]` — `fraction` is always a
  float (never `None`). Every stage (`upload`, `submit`, `wait`, `download`)
  is bracketed with `0.0` at start and `1.0` at end. Upload-chunk progress
  falls back to `0.0` when the total is unknown.
- `Upload` dataclass now also represents an existing row (returned from
  `uploads.list` / `uploads.get`) — `upload_url` is `None` for those rows,
  since the resumable session URL isn't replayable. Added `filename`,
  `file_size`, and `created_at` fields. Existing `upload_file()` signature
  unchanged.

### Removed
- Dropped support for Python 3.10 and 3.11. Minimum is now Python 3.12.

## [0.2.0] - 2026-05-21

### Added
- `client.samples.set_retention(...)`, `set_retention_bulk(...)`, and
  `restore(...)` for managing per-sample data-retention TTL (pin
  indefinitely, set an explicit `expires_at`, or fall back to org default).

## [0.1.0] - 2026-05-20

### Added
- Initial public release of the Python client for the Strand Platform API.
- `Client` with `api_key` / `base_url` / `timeout` / `max_retries` configuration
  and `STRAND_API_KEY` / `STRAND_BASE_URL` env-var fallback.
- `client.uploads.upload_file(...)` — resumable chunked upload to GCS for whole
  slide images.
- `client.predict.estimate(...)` and `client.predict.submit(...)` typed wrappers
  around the `/api/v1` REST endpoints.
- `client.predict(path, markers=..., output_dir=...)` convenience that runs the
  full pipeline (upload → submit → wait → download) in a single blocking call
  and returns a `PredictResult`.
- `Job.wait()` with SSE event streaming for live progress, and
  `Job.download_results()` returning either OME-Zarr paths or an `AnnData`
  object (with the `[anndata]` extra installed).
- Typed exception hierarchy: `JobFailedError`, `JobTimeoutError`,
  `InsufficientCreditsError`, `RateLimitError`, `AuthError`, `BadRequestError`,
  `NotFoundError`.
- Pinned `openapi.json` snapshot of the platform spec for drift-checking.

[0.3.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.3.0
[0.2.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.2.0
[0.1.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.1.0
