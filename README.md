# strand-sdk

Python client for the [Strand Platform](https://strandai.com) — H&E → multiplex protein inference.

```bash
pip install strand-sdk
# or with bioinformatics extras (AnnData / zarr):
pip install "strand-sdk[anndata]"
```

## Quickstart

```python
from strand import Client

client = Client()  # reads STRAND_API_KEY

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
  _predict.py        predict namespace
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

## License

Apache 2.0
