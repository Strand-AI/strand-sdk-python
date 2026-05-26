"""Thin HTTP helper around the generated `AuthenticatedClient`.

Centralizes:
- Constructing an `httpx.Client` with our auth header, base URL, and timeouts.
- Mapping documented error response bodies onto our typed exceptions.

We deliberately do not lean on the generated `client.AuthenticatedClient.with_*`
helpers — `httpx.Client` is plenty, and we want a single source of truth for
error mapping.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, cast

import httpx

from ._errors import (
    AuthError,
    BadRequestError,
    InsufficientCreditsError,
    NotFoundError,
    RateLimitError,
    StrandError,
    UnknownMarkerError,
)

DEFAULT_BASE_URL = "https://app.strandai.com"
DEFAULT_TIMEOUT = 60.0
USER_AGENT = "strand-sdk-python/0.2.0"


class HttpSession:
    """Wraps `httpx.Client` with Strand auth + error mapping."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float | httpx.Timeout | None,
        client: httpx.Client | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("STRAND_API_KEY")
        if not resolved_key:
            raise AuthError(
                "No API key provided. Pass api_key=... or set STRAND_API_KEY.",
                status_code=401,
                error_code="missing_api_key",
            )

        resolved_base = (
            base_url or os.environ.get("STRAND_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")

        self.api_key = resolved_key
        self.base_url = resolved_base
        self.api_root = f"{resolved_base}/api/v1"
        self._owned_client = client is None
        self._client = client or httpx.Client(
            base_url=self.api_root,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            follow_redirects=False,
        )

    # ---------- lifecycle ----------

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> HttpSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def client(self) -> httpx.Client:
        return self._client

    # ---------- request helpers ----------

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        resp = self._client.request(method, path, json=json, params=params)
        self._raise_for_error(resp)
        if resp.status_code not in expected:
            raise StrandError(
                f"Unexpected status {resp.status_code} for {method} {path}",
                status_code=resp.status_code,
                body=_safe_body(resp),
            )
        return cast(dict[str, Any], resp.json())

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> bytes:
        resp = self._client.request(method, path, params=params)
        self._raise_for_error(resp)
        return resp.content

    def stream_lines(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Iterator[bytes]:
        """Yield raw lines (including trailing newlines stripped) from a streaming response.

        Used for the SSE endpoint — callers wrap with httpx-sse for event parsing.
        """
        ctx_timeout = (
            timeout if timeout is not None else httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        )
        with self._client.stream(method, path, params=params, timeout=ctx_timeout) as resp:
            self._raise_for_error(resp)
            for line in resp.iter_lines():
                yield line.encode("utf-8") if isinstance(line, str) else line

    def stream_response(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        """Open a streaming response and return it; caller must call `.close()`.

        Used by the SSE wait loop with httpx-sse.
        """
        ctx_timeout = (
            timeout if timeout is not None else httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        )
        req = self._client.build_request(method, path, params=params, timeout=ctx_timeout)
        return self._client.send(req, stream=True)

    # ---------- error mapping ----------

    def _raise_for_error(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return

        body = _safe_body(resp)
        message = _extract_message(body, resp)
        error_code = body.get("error") if isinstance(body, dict) else None

        if resp.status_code == 400:
            if error_code == "unknown_markers" and isinstance(body, dict):
                unknown_raw = body.get("unknownMarkers")
                unknown = (
                    [str(m) for m in unknown_raw]
                    if isinstance(unknown_raw, list)
                    else []
                )
                known_raw = body.get("knownMarkersSample")
                known_subset = (
                    [str(m) for m in known_raw] if isinstance(known_raw, list) else None
                )
                raise UnknownMarkerError(
                    message,
                    unknown=unknown,
                    known_subset=known_subset,
                    body=body,
                )
            raise BadRequestError(message, status_code=400, error_code=error_code, body=body)
        if resp.status_code == 401:
            raise AuthError(message, status_code=401, error_code=error_code, body=body)
        if resp.status_code == 402:
            required = body.get("required") if isinstance(body, dict) else None
            raise InsufficientCreditsError(
                message,
                required=int(required) if isinstance(required, int) else None,
                body=body,
            )
        if resp.status_code == 404:
            raise NotFoundError(message, status_code=404, error_code=error_code, body=body)
        if resp.status_code == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw is not None else None
            except ValueError:
                retry_after = None
            raise RateLimitError(message, retry_after=retry_after, body=body)
        # 409 and other 4xx with a documented error shape — surface as generic StrandError.
        raise StrandError(
            message,
            status_code=resp.status_code,
            error_code=error_code,
            body=body,
        )


def _safe_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_message(body: dict[str, Any], resp: httpx.Response) -> str:
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, str) and msg:
            return msg
        err = body.get("error")
        if isinstance(err, str) and err:
            return err
    return f"HTTP {resp.status_code}"
