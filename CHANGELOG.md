# Changelog

All notable changes to `strand-sdk` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes on GitHub are extracted from the section header matching the
published version (e.g. `## [0.1.0]`), so keep headers in that exact form.

## [0.12.0] - 2026-08-06

### Added

- Added an `auto_segment` parameter to `client.uploads.upload_file(...)` and
  `client.predict(...)`. Cell segmentation still runs on ingest by default;
  pass `auto_segment=False` to skip it for that upload (the slide is still
  ingested and rendered), or `True` to force it even when the org default is
  off. `None` (the default) defers to the org's default. The resolved decision
  is surfaced on `Upload.auto_segment` from `uploads.get(...)` / `.list()`.

## [0.11.0] - 2026-08-04

### Added

- Added `client.samples.get(sample_id)`, the first sample-read endpoint. It
  returns a typed `Sample` (identity, status, physical scale, tags, and the
  expiration field group) so callers can check when a sample expires — via
  `.will_expire`, `.expires_in_days`, and the parsed `.expires_at` datetime —
  without the mutation that `set_expiration` requires. Previously there was no
  way to read a sample's expiration over the API without changing it.

## [0.10.0] - 2026-07-31

### Added

- Added `client.samples.list_tags(...)`, `client.samples.add_tag(...)`, and
  `client.samples.remove_tag(...)` for free-form, org-scoped sample tags —
  the same labels the dashboard shows. Previously tags were reachable only
  through the session-authed dashboard, so API-key clients could not label a
  cohort. `add_tag` is idempotent and `remove_tag` reports whether a tag was
  actually present.

## [0.9.0] - 2026-07-31

Reading a Lattice result worked on neither documented path before this release.
`0.8.0` is reserved for the in-review upload-contract change (#523); this
release sits on top of it.

### Fixed

- `to_array()` / `to_anndata()` now decode the zarr v3 `sharding_indexed`
  codec with zstd-compressed inner chunks — the layout the platform has
  actually written since marker pyramids moved to shards. Previously every
  real result raised `StrandError: Unsupported codec in zarr`.
- `download_to()` no longer aborts when the result manifest declares a pyramid
  level that storage doesn't hold. The store is treated as authoritative: the
  absent dataset is dropped from the mirrored `zarr.json` (so the local copy
  stays a valid, openable zarr store) and a `UserWarning` names it. This makes
  results written before the platform-side manifest fix readable as-is.
- Absent chunk/shard objects are read as `fill_value` rather than raising,
  matching zarr semantics.

### Changed

- The `anndata` extra now also installs `zstandard` (on Python < 3.14; 3.14+
  uses the stdlib `compression.zstd`). Array decoding needs a zstd binding.
## [0.8.0] - 2026-07-31

### Fixed

- `uploads.upload_file(...)` raised `KeyError: 'widthPx'` on every successful
  upload. Completion hands the sample to de-identification and does not return
  slide dimensions — those are read later, off the de-identified copy. The
  client now reads the completion response defensively; `width_px` /
  `height_px` stay `None` until a subsequent `uploads.get(...)`.

## [0.7.0] - 2026-07-30

### Added

- Added `client.samples.set_mpp(...)` for user-reported physical pixel-size
  overrides before inference.
- Added `Job.request_ome_tiff_export()`, `Job.get_ome_tiff_export()`, and
  `Job.export_ome_tiff(...)` for asynchronous OME-TIFF export and download.

## [0.6.0] - 2026-07-30

### Changed

- Added `"v0.7"` to the typed `ModelId` input surface and made the current
  default explicit in the SDK documentation.
- Added `"v0.6"` and `"v0.7"` to historical job/result model labels. v0.6 is
  intentionally output-only because it is sunset for new submissions.

## [0.5.1] - 2026-06-03

### Changed
- Dropped the legacy `"v10"` / `"v10-fullpanel"` / `"v10-fullpanel-v2"`
  alias-rewriting path. The SDK no longer emits a `DeprecationWarning`
  and no longer normalizes legacy strings client-side — they're forwarded
  verbatim to the server, which now returns 400 `unknown_model`. Pass
  `model="v0.4"` or `model="v0.5"` directly. The original 0.5.0 release
  notes (below) called out a 2026-12-01 sunset window for the alias
  path; we collapsed that to a hard cutover on 2026-06-03 after the
  in-the-wild traffic sample showed no callers still emitting the
  legacy strings. See `infra/notes/postman-versioning-2026-06.md` §4
  (rewritten 2026-06-03) in the platform repo.
- `JobStatus.model` and `PredictResult.model` are now typed as
  `Literal["v0.1", "v0.4", "v0.5"] | None`. The renumber of the sunset
  35-marker base from `"v0.3"` → `"v0.1"` (design note §8.2, locked
  2026-06-03) ships via a follow-up DB migration; historical jobs that
  ran on `wx0hp7fb` now surface as `"v0.1"` on the wire instead of
  `"v0.3"`.

### Migration

No action required if you already migrated to canonical v0.X ids per
0.5.0 below. If you're still passing `"v10*"` strings, expect a 400
`unknown_model` response and update the call to the canonical id.

## [0.5.0] - 2026-06-03

### Changed
- `ModelId` is now `Literal["v0.4", "v0.5"]` — the canonical Lattice
  version track from the platform's `POSTMAN_VERSIONS` map. The earlier
  `"v10"`, `"v10-fullpanel"`, and `"v10-fullpanel-v2"` strings are still
  accepted on input as deprecated aliases (each emits a
  `DeprecationWarning`); the SDK rewrites `"v10-fullpanel"` → `"v0.4"`
  and `"v10-fullpanel-v2"` → `"v0.5"` before sending. `"v10"` resolves
  to the now-sunset v0.3 and the server returns 400 `unknown_model`. The
  alias path will be removed on 2026-12-01. See
  `infra/notes/postman-versioning-2026-06.md` §4 in the platform repo.
- `JobStatus` and `PredictResult` now expose a `model` attribute typed
  as `Literal["v0.3", "v0.4", "v0.5"] | None`. The platform normalizes
  before persisting, so this is always the canonical v0.X label that
  ran — historical jobs may surface `"v0.3"`; newly submitted jobs
  return one of the live ids. `None` only for older servers that didn't
  populate the field.
- `ModelId` is re-exported from the package surface (`from strand import
  ModelId`).

### Migration

```python
# Before
job = client.predict.submit(upload.id, ["CD8"], model="v10-fullpanel-v2")
# After (no warning, future-proof through 2026-12-01)
job = client.predict.submit(upload.id, ["CD8"], model="v0.5")
```

Legacy strings keep working until 2026-12-01; the `DeprecationWarning`
is the only change visible to a caller that doesn't migrate.

## [0.4.1] - 2026-05-27

### Fixed
- `client.predict(...)` now uploads with `if_not_exists=True` so repeat calls
  on the same WSI dedup against the existing sample instead of re-uploading
  and racing the still-preprocessing prior upload on `submit()`. Previously a
  second `predict()` on the same file would 400 with "Sample is still
  preprocessing." (#95)
- `strand.__version__` is now resolved at import time from package metadata
  (`importlib.metadata.version("strand-sdk")`) instead of a hardcoded literal,
  so it can no longer drift from `pyproject.toml` between releases. Falls back
  to `"unknown"` when the distribution can't be located (e.g. running from a
  source tree without `pip install -e .`). (#98)

## [0.4.0] - 2026-05-27

### BREAKING

- **`client.samples.set_retention()` removed** — use `client.samples.set_expiration()`.
- **`client.samples.set_retention_bulk()` removed** — use `client.samples.set_expiration_bulk()`.
- The `pin=True` keyword is replaced by `never_expire=True`.
- REST endpoint paths renamed:
  - `PATCH /samples/{id}/retention` → `PATCH /samples/{id}/expiration`
  - `PATCH /samples/retention` → `PATCH /samples/expiration`
- Request body field `pin` → `neverExpire`.

No deprecation shim. The old names emit `AttributeError`.

### Migration

```python
# Before
client.samples.set_retention(sample_id, pin=True)
client.samples.set_retention_bulk([id1, id2], expires_at=date)
# After
client.samples.set_expiration(sample_id, never_expire=True)
client.samples.set_expiration_bulk([id1, id2], expires_at=date)
```

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

[0.6.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.6.0
[0.5.1]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.5.1
[0.5.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.5.0
[0.4.1]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.4.1
[0.4.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.4.0
[0.3.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.3.0
[0.2.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.2.0
[0.1.0]: https://github.com/Strand-AI/strand-official/releases/tag/sdk-python%2Fv0.1.0
