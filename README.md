# strand-sdk

Python client for the [Strand Platform](https://strandai.com): upload H&E
whole-slide images, run virtual multiplex-immunofluorescence inference
(H&E → spatial proteomics), and download per-marker predictions as AnnData
or OME-Zarr/OME-TIFF.

📚 **Full documentation: <https://docs.strandai.com/sdks/python>**

Agent-readable API reference: <https://app.strandai.com/docs/api.md> · LLM
index: <https://app.strandai.com/llms.txt>

Requires Python 3.12+.

## Install

```bash
pip install strand-sdk
# or with bioinformatics extras (AnnData / zarr):
pip install "strand-sdk[anndata]"
```

## Quickstart

One blocking call runs the full pipeline — upload, submit, wait, download:

```python
from strand import Client

client = Client()  # reads STRAND_API_KEY; mint one at https://app.strandai.com/settings/api-keys
result = client.predict(
    "biopsy.ome.tiff",
    markers=["HER2", "CD8", "PD1"],
    output_dir="./outputs/",
)
print(f"Used {result.credits_used} credits; wrote {len(result.marker_outputs)} markers")
```

Everything else — uploads, model selection, async jobs, OME-TIFF export,
error handling — is covered in the
[hosted docs](https://docs.strandai.com/sdks/python).

## Issues & support

File bug reports and feature requests at
[Strand-AI/strand-sdk-python/issues](https://github.com/Strand-AI/strand-sdk-python/issues),
or email [support@strandai.com](mailto:support@strandai.com). This repository
is a generated, read-only mirror of Strand's monorepo — pull requests opened
here are overwritten by the next sync.

## License

Apache 2.0
