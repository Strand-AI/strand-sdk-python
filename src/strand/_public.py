"""Public-cohort reads — the free, credit-less `client.public` namespace.

Any authenticated org can browse and read Strand's curated public cohort (the
TCGA release) for free — it is org-independent, so you read it regardless of
which org your credential belongs to, and no read reserves credits. Mirrors:

    GET /api/v1/public/samples                       -> list(...)
    GET /api/v1/public/samples/{publicId}            -> get(public_id)
    GET /api/v1/public/samples/{publicId}/zarr/{...} -> PublicSample.download_to(...)

Generation stays credit-gated and lives on `client.predict` / `client.uploads`;
this namespace is read-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._errors import StrandError
from ._models import PublicSampleGeometry, PublicSampleList
from ._results import mirror_zarr_store

if TYPE_CHECKING:
    from ._http import HttpSession


class PublicSample:
    """Handle for one public-cohort sample (`client.public.get(public_id)`).

    Carries the curated detail — `title`, `tags`, `metadata`, `geometry`, and
    the live `markers` — and reads the sample's OME-Zarr pyramid (H&E + every
    marker channel) through the authenticated public byte proxy. Use
    `download_to(dir)` to mirror the whole store locally.
    """

    def __init__(
        self,
        *,
        http: HttpSession,
        public_id: str,
        title: str,
        tags: list[str],
        metadata: dict[str, Any],
        geometry: PublicSampleGeometry,
        markers: list[str],
        thumbnail_url: str,
        pyramid_url: str,
    ) -> None:
        self._http = http
        self.public_id = public_id
        self.title = title
        self.tags = tags
        self.metadata = metadata
        self.geometry = geometry
        self.markers = markers
        self.thumbnail_url = thumbnail_url
        self.pyramid_url = pyramid_url
        self._root_cache: dict[str, Any] | None = None

    @classmethod
    def _from_dict(cls, http: HttpSession, raw: dict[str, Any]) -> PublicSample:
        viewer = raw.get("viewer") or {}
        markers = [
            str(m["name"])
            for m in viewer.get("markers", [])
            if isinstance(m, dict) and m.get("name")
        ]
        return cls(
            http=http,
            public_id=str(raw["publicId"]),
            title=str(raw.get("title", "")),
            tags=[str(t) for t in raw.get("tags", [])],
            metadata=dict(raw.get("metadata") or {}),
            geometry=PublicSampleGeometry._from_dict(raw.get("geometry") or {}),
            markers=markers,
            thumbnail_url=str(raw.get("thumbnailUrl", "")),
            pyramid_url=str(viewer.get("pyramidUrl", "")),
        )

    # ---------- zarr byte access via the public proxy ----------

    def _zarr_path(self, path: str) -> str:
        rel = path.strip("/")
        base = f"/public/samples/{self.public_id}/zarr"
        return f"{base}/{rel}" if rel else f"{base}/"

    def get_bytes(self, path: str) -> bytes:
        """Fetch one object from the sample's zarr store (e.g. `he/0/c/0/0/0`)."""
        return self._http.request_bytes("GET", self._zarr_path(path))

    def get_json(self, path: str) -> dict[str, Any]:
        """Fetch and parse one JSON node from the zarr store (e.g. `zarr.json`)."""
        data = json.loads(self.get_bytes(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise StrandError(f"Expected JSON object at {path}, got {type(data).__name__}")
        return data

    def root_meta(self) -> dict[str, Any]:
        """The root group's `zarr.json` (H&E + every marker multiscale), cached."""
        if self._root_cache is None:
            self._root_cache = self.get_json("zarr.json")
        return self._root_cache

    def download_to(self, target: str | Path) -> Path:
        """Mirror the sample's entire OME-Zarr store (H&E + markers) to `target/`.

        This materializes the actual marker pixel data. The result is a valid
        zarr v3 store readable by `zarr.open(target)`; missing chunks are left
        absent (read as `fill_value`). Returns the `target` path.
        """
        return mirror_zarr_store(self.root_meta(), self.get_json, self.get_bytes, target)


class PublicSamples:
    """`client.public` namespace — read the curated public cohort for free."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def list(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
        tag: str | None = None,
    ) -> PublicSampleList:
        """List the public cohort (paginated, newest-first).

        Args:
            page: 1-based page number (defaults to 1).
            page_size: Items per page (server default 48, max 100).
            tag: Optional public display-tag filter. An unknown tag returns an
                empty page.

        Returns:
            A `PublicSampleList` page of `PublicSampleSummary` cards.
        """
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["pageSize"] = page_size
        if tag is not None:
            params["tag"] = tag
        payload = self._http.request_json("GET", "/public/samples", params=params or None)
        return PublicSampleList._from_dict(payload)

    def get(self, public_id: str) -> PublicSample:
        """Fetch one public sample's detail as a `PublicSample` handle.

        Args:
            public_id: The sample's public id (from `list(...)`).

        Returns:
            A `PublicSample` — read its `markers` / `geometry`, or call
            `download_to(dir)` to materialize the marker data.

        Raises:
            NotFoundError: No currently-public sample matches `public_id`.
        """
        payload = self._http.request_json("GET", f"/public/samples/{public_id}")
        return PublicSample._from_dict(self._http, payload)
