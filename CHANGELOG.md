# Changelog

All notable changes to `strand-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes on GitHub are extracted from the section header matching the
published version (e.g. `## [0.1.0]`), so keep headers in that exact form.

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

[0.1.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.1.0
