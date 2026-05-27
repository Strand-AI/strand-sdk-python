# strand-sdk

Python client for the [Strand Platform](https://strandai.com) — H&E → multiplex protein inference.

**Agent-friendly docs:** The full API reference is published as Markdown at [https://app.strandai.com/docs/api.md](https://app.strandai.com/docs/api.md), and the LLM index lives at [https://app.strandai.com/llms.txt](https://app.strandai.com/llms.txt).

```bash
pip install strand-sdk
# or with bioinformatics extras (AnnData / zarr):
pip install "strand-sdk[anndata]"
```

If your environment can't reach PyPI, you can install directly from the
repository as a fallback:

```bash
pip install "git+https://github.com/Strand-AI/strand-sdk-python.git"
```

## Quickstart

One blocking call runs the full pipeline — upload, submit, wait, download:

```python
from strand import Client

client = Client(api_key="sk-strand-...")
result = client.predict(
    "biopsy.ome.tiff",
    markers=["HER2", "CD8", "PD1"],
    output_dir="./outputs/",
)
print(f"Used {result.credits_used} credits; wrote {len(result.marker_outputs)} markers")
```

`client.predict(...)` returns a `PredictResult` with `job_id`, `status`,
`credits_used`, `marker_outputs` (paths under `output_dir`), and `results`
(a `JobResults` handle for selective reads). It raises `JobFailedError` if the
job fails, `JobTimeoutError` if the deadline elapses, and surfaces
`InsufficientCreditsError` / `RateLimitError` on submit issues.

Pass `on_progress=lambda stage, frac: ...` to follow the four stages
(`"upload"`, `"submit"`, `"wait"`, `"download"`). `frac` is always a float
in `[0.0, 1.0]` — `0.0` at stage start, `1.0` at stage end, with
intermediate values where available (e.g. upload byte progress).

### Fire-and-forget: `wait=False`

A full pipeline run blocks for 15+ minutes. To kick a job off and do other
work in the meantime, pass `wait=False` — `predict(...)` returns a `Job`
handle as soon as the upload + submit complete:

```python
job = client.predict("slide.svs", markers=["CD3", "CD8"], wait=False)
print(f"submitted {job.id}")

# ...do other work, or shut down the process — the job runs server-side.

# Later (same process or a fresh one via `client.jobs.get(job_id)`):
job.wait()
result = job.download_results()      # AnnData
# or stream events:
for event in job.stream_events():
    print(event.status, event.progress)
```

Static typing follows the `wait` flag — `wait=True` returns `PredictResult`,
`wait=False` returns `Job`, so IDE completions stay correct without a
runtime check.

### Choosing a model

`predict.submit` and `predict(...)` accept an optional `model=`. Today the
SDK routes to two siblings of the POSTMAN v10 family:

- `"v10"` — original 7-marker panel (smaller, faster).
- `"v10-fullpanel"` — 192-marker panel (broader coverage).

Both share the same GenePT embeddings, so the marker vocabulary is identical
— picking a model is a model-weights swap, not a vocab swap. Omitting `model`
lets the platform pick the default (currently `v10-fullpanel`).

```python
result = client.predict(
    "slide.svs",
    markers=["CD8", "Ki67", "PanCK"],
    model="v10-fullpanel",
)
```

### Recovering from a failed pipeline without re-uploading

If `client.predict(...)` raises after the upload step succeeded, the
resulting `upload_id` is attached to the error so you can resume the job
without paying to re-upload the WSI:

```python
from strand import Client, JobFailedError, StrandError

client = Client()
try:
    result = client.predict("slide.svs", markers=["CD3", "CD8"])
except JobFailedError as e:
    if e.upload_id:
        # Re-submit against the same upload — no re-upload needed.
        job = client.predict.submit(e.upload_id, markers=["CD3", "CD8"])
        job.wait()
except StrandError as e:
    # Same idea for `JobTimeoutError`, `InsufficientCreditsError`, etc.
    if e.upload_id:
        ...
```

### Catching unknown markers

`predict.submit` and `predict.estimate` validate marker names against the
platform's panel before reserving credits or queueing a job. Unknown names
surface as `UnknownMarkerError` (a `BadRequestError` subclass):

```python
from strand import UnknownMarkerError

try:
    client.predict.submit(upload.id, markers=["CD3", "MysteryMarker"])
except UnknownMarkerError as e:
    print("Unknown:", e.unknown)              # ["MysteryMarker"]
    print("Try one of:", e.known_subset[:5])  # sample of valid names
```

### Lower-level primitives

`client.predict` is also a namespace, so the underlying steps stay available
for fine-grained control:

```python
upload = client.uploads.upload_file("slide.svs")
estimate = client.predict.estimate(upload.id, markers=["CD3", "CD8", "Ki67"])
print(f"Will cost ≈ {estimate.estimated_credits} credits")

job = client.predict.submit(upload.id, markers=["CD3", "CD8", "Ki67"])
job.wait()                                    # blocks until terminal status
adata = job.download_results()                # AnnData
```

## Configuration

| Source | Variable / argument | Default |
|---|---|---|
| Env | `STRAND_API_KEY` | required |
| Env | `STRAND_BASE_URL` | `https://app.strandai.com` |
| Arg | `Client(api_key=..., base_url=..., timeout=..., max_retries=...)` | — |

## Layout

```
src/strand/
  __init__.py        public surface re-exports
  _client.py         Client (top-level)
  _uploads.py        uploads namespace (incl. resumable chunked upload helper)
  _predict.py        predict namespace — `client.predict(...)` (full pipeline) + `.estimate` / `.submit`
  _jobs.py           Job (wait / stream_events / download_results)
  _results.py        OME-Zarr v3 download + AnnData conversion
  _models.py         user-facing snake_case dataclasses
  _http.py           internal httpx wrapper with typed error mapping
  _errors.py         typed exceptions
openapi.json         pinned snapshot of the platform spec (drift-check)
```

## Verifying against the platform OpenAPI spec

Transport is hand-written for ergonomic snake_case fields and AnnData
integration. To check the SDK against an updated spec:

```bash
# regenerate a reference client and diff the request/response surface
uv tool run --from "openapi-python-client>=0.21" --with "click<8.2" \
    openapi-python-client generate \
    --path openapi.json \
    --output-path /tmp/strand-sdk-ref \
    --meta none --overwrite
```

To refresh `openapi.json` itself against a live server:

```bash
curl https://app.strandai.com/api/v1/openapi.json -o openapi.json
# or against local dev:
# curl http://localhost:3000/api/v1/openapi.json -o openapi.json
```

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check src tests
uv run mypy src
```

## Issues & contributing

File bug reports and feature requests at
[Strand-AI/strand-sdk-python/issues](https://github.com/Strand-AI/strand-sdk-python/issues).

We don't accept external pull requests on the SDK at this time. If you'd like
to contribute or have ideas you'd like to discuss, email
[support@strandai.com](mailto:support@strandai.com).

## License

Apache 2.0
