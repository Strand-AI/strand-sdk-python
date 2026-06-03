"""Strand Platform Python SDK.

Quickstart — one-call pipeline:

    >>> from strand import Client
    >>> client = Client()  # reads STRAND_API_KEY
    >>> result = client.predict(
    ...     "slide.svs",
    ...     markers=["CD3", "CD8"],
    ...     output_dir="./outputs/",
    ... )
    >>> print(f"used {result.credits_used} credits")

Lower-level primitives stay available for fine-grained control:

    >>> upload = client.uploads.upload_file("slide.svs")
    >>> job = client.predict.submit(upload.id, markers=["CD3", "CD8"])
    >>> job.wait()
    >>> adata = job.download_results()

See `https://app.strandai.com/docs/api` for the underlying REST API reference.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from ._client import Client
from ._errors import (
    AuthError,
    BadRequestError,
    InsufficientCreditsError,
    JobFailedError,
    JobTimeoutError,
    NotFoundError,
    RateLimitError,
    StrandError,
    UnknownMarkerError,
    UploadError,
)
from ._jobs import Job, JobEvent
from ._models import Estimate, JobStatus, PredictResult, Upload
from ._predict import ModelId
from ._results import JobResults
from ._uploads import UploadList

__all__ = [
    "AuthError",
    "BadRequestError",
    "Client",
    "Estimate",
    "InsufficientCreditsError",
    "Job",
    "JobEvent",
    "JobFailedError",
    "JobResults",
    "JobStatus",
    "JobTimeoutError",
    "ModelId",
    "NotFoundError",
    "PredictResult",
    "RateLimitError",
    "StrandError",
    "UnknownMarkerError",
    "Upload",
    "UploadError",
    "UploadList",
]

try:
    __version__ = _pkg_version("strand-sdk")
except PackageNotFoundError:
    __version__ = "unknown"
