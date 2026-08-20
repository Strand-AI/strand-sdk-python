"""Download-capable handle for a public sample resolved by `client.samples.get`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ._errors import StrandError
from ._models import PublicSampleGeometry
from ._results import mirror_zarr_store

if TYPE_CHECKING:
    from ._http import HttpSession


class PublicSample:
    """Public sample detail with authenticated OME-Zarr byte access.

    Instances come from `client.samples.get(public_id)`. The public share id is
    exposed as canonical `id`; `download_to(dir)` mirrors the complete H&E and
    marker store through the retained v1 public byte routes.
    """

    ownership: Literal["public"] = "public"

    def __init__(
        self,
        *,
        http: HttpSession,
        id: str,
        title: str,
        tags: list[str],
        metadata: dict[str, Any],
        geometry: PublicSampleGeometry,
        markers: list[str],
        thumbnail_url: str,
        pyramid_url: str,
    ) -> None:
        self._http = http
        self.id = id
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
            str(marker["name"])
            for marker in viewer.get("markers", [])
            if isinstance(marker, dict) and marker.get("name")
        ]
        return cls(
            http=http,
            id=str(raw["id"]),
            title=str(raw["title"]),
            tags=[str(tag) for tag in raw.get("tags", [])],
            metadata=dict(raw.get("metadata") or {}),
            geometry=PublicSampleGeometry._from_dict(raw.get("geometry") or {}),
            markers=markers,
            thumbnail_url=str(raw["thumbnailUrl"]),
            pyramid_url=str(viewer["pyramidUrl"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the serializable, public-only detail represented by this handle."""
        return {
            "ownership": self.ownership,
            "id": self.id,
            "title": self.title,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "geometry": {
                "width_px": self.geometry.width_px,
                "height_px": self.geometry.height_px,
                "mpp_x": self.geometry.mpp_x,
                "mpp_y": self.geometry.mpp_y,
            },
            "markers": list(self.markers),
            "thumbnail_url": self.thumbnail_url,
            "pyramid_url": self.pyramid_url,
        }

    def _zarr_path(self, path: str) -> str:
        rel = path.strip("/")
        base = f"/public/samples/{self.id}/zarr"
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
        """Return the cached root `zarr.json` group for H&E and marker multiscales."""
        if self._root_cache is None:
            self._root_cache = self.get_json("zarr.json")
        return self._root_cache

    def download_to(self, target: str | Path) -> Path:
        """Mirror the sample's complete OME-Zarr store to `target`."""
        return mirror_zarr_store(self.root_meta(), self.get_json, self.get_bytes, target)
