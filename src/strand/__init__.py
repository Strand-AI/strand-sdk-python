"""Strand Platform Python SDK.

Quickstart:

    >>> from strand import Client
    >>> client = Client()  # reads STRAND_API_KEY
    >>> upload = client.uploads.upload_file("slide.svs")
    >>> job = client.predict.submit(upload.id, markers=["CD3", "CD8"])
    >>> job.wait()
    >>> adata = job.download_results()

See `https://app.strandai.com/docs/api` for the underlying REST API reference.
"""

from __future__ import annotations

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
    UploadError,
)
from ._jobs import Job, JobEvent
from ._models import Estimate, JobStatus, Upload
from ._results import JobResults

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
    "NotFoundError",
    "RateLimitError",
    "StrandError",
    "Upload",
    "UploadError",
]

__version__ = "0.1.0"
