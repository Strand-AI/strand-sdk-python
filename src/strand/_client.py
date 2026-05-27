"""Top-level `Client` — entry point for the SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ._http import DEFAULT_TIMEOUT, HttpSession
from ._predict import Predict
from ._samples import Samples
from ._uploads import Uploads

if TYPE_CHECKING:
    from ._jobs import Job


class _JobsNamespace:
    """`client.jobs` namespace — fetch / look up jobs by id."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def get(self, job_id: str) -> Job:
        """Return a `Job` handle and pre-populate its cached status."""
        from ._jobs import Job

        job = Job(id=job_id, reserved_credits=None, client=self._client)
        job.refresh()
        return job

    def cancel(self, job_id: str) -> Job:
        """Cancel an in-flight job by id and return its refreshed handle.

        Convenience for ``client.jobs.get(job_id).cancel()``. Raises
        :class:`BadRequestError` if the job is already terminal.
        """
        from ._jobs import Job

        job = Job(id=job_id, reserved_credits=None, client=self._client)
        job.cancel()
        return job


class Client:
    """Strand Platform API client.

    Args:
        api_key: API key (`sk-strand-...`). Falls back to `STRAND_API_KEY` env var.
        base_url: API base URL. Defaults to `STRAND_BASE_URL` env var, else
            `https://app.strandai.com`. Should not include the `/api/v1` suffix.
        timeout: Per-request timeout in seconds (or an `httpx.Timeout`).
        http_client: Pre-built `httpx.Client` for advanced use (e.g., custom
            transport, retries, ASGI mounting in tests). The SDK will NOT
            override the client's `Authorization` header — if you pass one,
            wire auth headers yourself.

    Example — one-call pipeline:

        >>> client = Client(api_key="sk-strand-...")
        >>> result = client.predict(
        ...     "slide.svs",
        ...     markers=["CD3", "CD8"],
        ...     output_dir="./outputs/",
        ... )
        >>> print(f"used {result.credits_used} credits")

    Lower-level primitives (`client.predict` is also a namespace):

        >>> upload = client.uploads.upload_file("slide.svs")
        >>> estimate = client.predict.estimate(upload.id, markers=["CD3"])
        >>> job = client.predict.submit(upload.id, markers=["CD3"])
        >>> job.wait()
        >>> adata = job.download_results()
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._http = HttpSession(
            api_key=api_key, base_url=base_url, timeout=timeout, client=http_client
        )
        self.uploads = Uploads(self._http)
        self.predict = Predict(self._http, self)
        self.jobs = _JobsNamespace(self)
        self.samples = Samples(self._http)

    @property
    def base_url(self) -> str:
        return self._http.base_url

    @property
    def api_root(self) -> str:
        return self._http.api_root

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
