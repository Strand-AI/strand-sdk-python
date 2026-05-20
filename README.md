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
(`"upload"`, `"submit"`, `"wait"`, `"download"`).

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

This SDK is published from a read-only public mirror of an internal monorepo.
File bug reports and feature requests on the mirror's issue tracker:
[github.com/Strand-AI/strand-sdk-python/issues](https://github.com/Strand-AI/strand-sdk-python/issues).

The mirror doesn't accept pull requests directly — changes land in the
internal monorepo and are synced here automatically on every release.

## License

Apache 2.0
