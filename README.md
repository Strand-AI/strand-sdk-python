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

The package also installs the `strand` command. It supports the same sample
and run lifecycle without renaming the SDK-native verbs:

```bash
sample_id=$(strand upload biopsy.svs --mpp 0.26 | jq -r .id)
while :; do
  status=$(strand samples get "$sample_id" | jq -r .status)
  case "$status" in ready) break ;; *failed) exit 1 ;; esac
  sleep 2
done
strand predict "$sample_id" -m CD8,PanCK --dry-run
job_id=$(strand predict "$sample_id" -m CD8,PanCK | jq -r .job_id)
strand wait "$job_id" --timeout 1800
strand export "$job_id" --format ome-tiff --out result.ome.tiff --timeout 1800
```

Use `strand cancel "$job_id"` for an eligible in-flight run and `strand
results "$job_id" --out ./results/` for the OME-Zarr tree.

## Quickstart

One blocking call runs the full pipeline: upload, submit, wait, and download.

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

Scoped sample reads and updates use one namespace:

```python
page = client.samples.list(scope="mine", tag="trial-042")
sample_id = page.items[0].id
sample = client.samples.get(sample_id)
client.samples.patch(sample_id, name="Baseline", tags=["trial-042"], mpp=0.26)

estimate = client.predict.submit(sample_id, ["CD8", "PanCK"], dry_run=True)
job = client.predict.submit(sample_id, ["CD8", "PanCK"])
```

Uploads, model selection, async jobs, public OME-Zarr reads, OME-TIFF export,
and error handling are covered in the
[hosted docs](https://docs.strandai.com/sdks/python).

## Issues & support

File bug reports and feature requests at
[Strand-AI/strand-sdk-python/issues](https://github.com/Strand-AI/strand-sdk-python/issues),
or email [support@strandai.com](mailto:support@strandai.com). This repository
is a generated, read-only mirror of Strand's monorepo. Pull requests opened
here are overwritten by the next sync.

## License

Apache 2.0
