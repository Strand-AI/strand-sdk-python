from __future__ import annotations

import respx
from httpx import Response

import strand

API_ROOT = "https://test.strandai.example/api/v1"
SAMPLE = "33333333-3333-4333-8333-333333333333"
LAYER = "44444444-4444-4444-8444-444444444444"


@respx.mock
def test_segmentation_lifecycle_start_and_manifest(client: strand.Client) -> None:
    lifecycle = {
        "status": "failed",
        "retryable": True,
        "creditCost": 0,
        "job": {"id": "seg-job", "status": "failed"},
        "layer": None,
    }
    queued = {**lifecycle, "status": "queued", "retryable": False, "job": None}
    manifest = {
        "schemaVersion": "1.0",
        "layerId": LAYER,
        "artifacts": {
            "mask": {"format": "ome-zarr-v3", "baseUrl": ".../mask"},
            "cells": {"format": "parquet", "joinKey": "instance_id"},
        },
    }
    respx.get(f"{API_ROOT}/samples/{SAMPLE}/segmentation").mock(
        return_value=Response(200, json=lifecycle)
    )
    respx.post(f"{API_ROOT}/samples/{SAMPLE}/segmentation").mock(
        return_value=Response(202, json=queued)
    )
    respx.get(f"{API_ROOT}/samples/{SAMPLE}/segmentation/{LAYER}/manifest").mock(
        return_value=Response(200, json=manifest)
    )

    assert client.samples.segmentation(SAMPLE).retryable is True
    assert client.samples.segment(SAMPLE).status == "queued"
    assert client.samples.segmentation_manifest(SAMPLE, LAYER)["artifacts"]["cells"]["joinKey"] == "instance_id"


@respx.mock
def test_segmentation_file_reads_authenticated_bytes(client: strand.Client) -> None:
    route = respx.get(
        f"{API_ROOT}/samples/{SAMPLE}/segmentation/{LAYER}/files/features/markers/CD3e.parquet"
    ).mock(return_value=Response(200, content=b"PAR1"))
    assert client.samples.segmentation_file(
        SAMPLE, LAYER, "features/markers/CD3e.parquet"
    ) == b"PAR1"
    assert route.called
