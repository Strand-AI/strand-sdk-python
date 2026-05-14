"""Results download + AnnData conversion.

The platform writes an OME-Zarr v3 group at `<resultBasePath>`. The root group
holds one `multiscales` entry per modality (the source H&E image plus one per
predicted marker), each pointing at its own pyramid:

    zarr.json                     root group (omero/multiscales metadata)
    he/{level}/zarr.json          H&E source array per pyramid level
    he/{level}/c/0/{cr}/{cc}      chunk bytes
    markers/{name}/{level}/...    one [1, H, W] array per marker (per level)

Chunks are little-endian raw bytes (codec="bytes" — no compression).

The SDK reads zarr directly via numpy — no dependency on zarr-python.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._errors import StrandError

if TYPE_CHECKING:
    from ._client import Client


_DTYPE_BYTES = {
    "uint8": 1,
    "int8": 1,
    "uint16": 2,
    "int16": 2,
    "uint32": 4,
    "int32": 4,
    "float32": 4,
    "float64": 8,
}


@dataclass(frozen=True, slots=True)
class _DatasetRef:
    """One pyramid level of one named multiscale (e.g. `("CD3", 0, "markers/CD3/0")`)."""

    name: str
    level: int
    path: str


class JobResults:
    """Handle for a completed job's OME-Zarr result store.

    Holds metadata about the result store; chunk downloads go through the
    API-key-authenticated proxy at `/api/v1/jobs/{id}/results/files/{path}`.
    """

    HE_NAME = "H&E"

    def __init__(
        self,
        *,
        job_id: str,
        result_url: str,
        result_base_path: str,
        expires_at: str,
        client: Client,
    ) -> None:
        self.job_id = job_id
        self.result_url = result_url
        self.result_base_path = result_base_path
        self.expires_at = expires_at
        self._client = client
        self._http = client._http
        self._root_cache: dict[str, Any] | None = None

    # ---------- file-level access via proxy ----------

    def _files_path(self, *parts: str) -> str:
        joined = "/".join(p.strip("/") for p in parts if p)
        return (
            f"/jobs/{self.job_id}/results/files/{joined}"
            if joined
            else f"/jobs/{self.job_id}/results/files/"
        )

    def get_bytes(self, path: str) -> bytes:
        return self._http.request_bytes("GET", self._files_path(path))

    def get_json(self, path: str) -> dict[str, Any]:
        data = json.loads(self.get_bytes(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise StrandError(f"Expected JSON object at {path}, got {type(data).__name__}")
        return data

    def root_meta(self) -> dict[str, Any]:
        if self._root_cache is None:
            self._root_cache = self.get_json("zarr.json")
        return self._root_cache

    # ---------- discovery ----------

    def multiscale_names(self, *, include_he: bool = False) -> list[str]:
        """Return the names of all multiscales — markers (and H&E if requested)."""
        names = [ms.get("name") for ms in _multiscales(self.root_meta())]
        out = [str(n) for n in names if isinstance(n, str)]
        if not include_he:
            out = [n for n in out if n != self.HE_NAME]
        return out

    def datasets(self, *, name: str | None = None, level: int = 0) -> list[_DatasetRef]:
        """Resolve `(name, level)` → dataset path. Pass `name=None` for all markers.

        Returns one `_DatasetRef` per resolved (name, level). Internal helper —
        callers usually want `to_array(name=...)` or `to_anndata(markers=...)`.
        """
        out: list[_DatasetRef] = []
        for ms in _multiscales(self.root_meta()):
            ms_name = ms.get("name")
            if name is not None and ms_name != name:
                continue
            datasets = ms.get("datasets") or []
            if level < 0 or level >= len(datasets):
                raise StrandError(
                    f"Level {level} out of range for multiscale {ms_name!r} "
                    f"(have {len(datasets)} levels)",
                )
            path = datasets[level].get("path")
            if not isinstance(path, str):
                raise StrandError(f"Missing dataset path for multiscale {ms_name!r}")
            out.append(_DatasetRef(name=str(ms_name or ""), level=level, path=path))
        return out

    # ---------- whole-store mirror ----------

    def download_to(self, target: str | Path) -> Path:
        """Mirror the entire zarr store to `target/`.

        Walks every multiscale's every level and copies the bytes locally.
        Returns the `target` path.
        """
        out = Path(target)
        out.mkdir(parents=True, exist_ok=True)

        root = self.root_meta()
        (out / "zarr.json").write_bytes(json.dumps(root).encode("utf-8"))

        for ms in _multiscales(root):
            for dataset in ms.get("datasets") or []:
                path = dataset.get("path")
                if not isinstance(path, str):
                    continue
                array_meta = self.get_json(f"{path}/zarr.json")
                dest = out / path
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "zarr.json").write_bytes(json.dumps(array_meta).encode("utf-8"))
                for chunk_path in _enumerate_chunks(array_meta):
                    file_path = f"{path}/{chunk_path}"
                    full = out / file_path
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_bytes(self.get_bytes(file_path))
        return out

    # ---------- per-array decode ----------

    def to_array(
        self,
        *,
        name: str | None = None,
        level: int = 0,
    ) -> tuple[Any, dict[str, Any]]:
        """Download a single named multiscale at `level` as a numpy `[C, H, W]` array.

        Args:
            name: Multiscale name (e.g. `"CD3"`). When omitted, defaults to
                the first marker (excluding H&E).
            level: Pyramid level. `0` is full-res.

        Returns:
            `(array, array_meta)` — `array` is `[C, H, W]` (typically C=1 for
            markers, C=3 for H&E).
        """
        try:
            import numpy as np
        except ImportError as exc:
            raise StrandError(
                "to_array() requires numpy. Install with: pip install 'strand-sdk[anndata]'",
            ) from exc

        target_name = name
        if target_name is None:
            markers = self.multiscale_names(include_he=False)
            if not markers:
                raise StrandError(
                    "No marker multiscales in result; pass name=... to read H&E",
                )
            target_name = markers[0]

        refs = self.datasets(name=target_name, level=level)
        if not refs:
            raise StrandError(f"No multiscale named {target_name!r} in result")
        ref = refs[0]
        meta = self.get_json(f"{ref.path}/zarr.json")
        return _read_array(meta, lambda chunk: self.get_bytes(f"{ref.path}/{chunk}"), np), meta

    def to_anndata(
        self,
        *,
        markers: Iterable[str] | None = None,
        level: int = 0,
    ) -> Any:
        """Return result as an `AnnData` of shape `(H*W, n_markers)`.

        Each pixel is an observation; each marker is a variable.
        `obsm["spatial"]` holds integer pixel `(x, y)` coordinates (scverse convention).
        For whole-slide outputs prefer `download_to(path)` and read selectively.

        Args:
            markers: Names of multiscales to include as variables. Defaults to all
                non-H&E multiscales, in the order the platform recorded them.
            level: Pyramid level; `0` is full-res.
        """
        try:
            import anndata as ad
            import numpy as np
        except ImportError as exc:
            raise StrandError(
                "to_anndata() requires anndata + numpy. Install with: pip install 'strand-sdk[anndata]'",
            ) from exc

        names = list(markers) if markers is not None else self.multiscale_names(include_he=False)
        if not names:
            raise StrandError("No marker multiscales to assemble into AnnData")

        h_dim = 0
        w_dim = 0
        channels: list[Any] = []
        for i, marker_name in enumerate(names):
            arr, _meta = self.to_array(name=marker_name, level=level)
            if arr.ndim != 3 or arr.shape[0] != 1:
                raise StrandError(
                    f"Expected marker arrays shaped [1, H, W]; {marker_name!r} is {arr.shape}",
                )
            if i == 0:
                _, h_dim, w_dim = (int(d) for d in arr.shape)
            elif int(arr.shape[1]) != h_dim or int(arr.shape[2]) != w_dim:
                raise StrandError(
                    f"Marker arrays have mismatched HxW: {marker_name!r} is "
                    f"{arr.shape[1]}x{arr.shape[2]}, others are {h_dim}x{w_dim}",
                )
            channels.append(arr[0])  # [H, W]

        stack = np.stack(channels, axis=0)  # [C, H, W]
        x = stack.transpose(1, 2, 0).reshape(h_dim * w_dim, len(names)).copy()

        yy, xx = np.meshgrid(np.arange(h_dim), np.arange(w_dim), indexing="ij")
        spatial = np.stack([xx.ravel(), yy.ravel()], axis=1)

        return ad.AnnData(
            X=x,
            var={"channel": names},
            obsm={"spatial": spatial},
            uns={
                "strand": {
                    "job_id": self.job_id,
                    "result_base_path": self.result_base_path,
                    "level": int(level),
                    "shape_chw": [len(names), h_dim, w_dim],
                }
            },
        )


# ---------- helpers ----------


def _multiscales(root_meta: dict[str, Any]) -> list[dict[str, Any]]:
    attrs = root_meta.get("attributes") or {}
    ms = attrs.get("multiscales") or []
    return [m for m in ms if isinstance(m, dict)]


def _read_array(meta: dict[str, Any], fetch_chunk: Any, np: Any) -> Any:
    shape = list(meta["shape"])
    chunk_shape = list(meta["chunk_grid"]["configuration"]["chunk_shape"])
    dtype_name = str(meta["data_type"])
    if dtype_name not in _DTYPE_BYTES:
        raise StrandError(f"Unsupported dtype in zarr: {dtype_name!r}")
    codecs = meta.get("codecs", [])
    if any(c.get("name") not in {"bytes"} for c in codecs):
        raise StrandError(
            f"Unsupported codec in zarr (only 'bytes' is supported): {codecs!r}"
        )
    if len(shape) != 3:
        raise StrandError(f"Expected 3-dim [C, H, W] array, got shape {shape}")

    c_dim, h_dim, w_dim = shape
    chunk_c, chunk_h, chunk_w = chunk_shape
    if chunk_c != c_dim:
        raise StrandError(
            "SDK currently expects all channels in a single chunk along C; "
            f"got chunks={chunk_shape}, shape={shape}.",
        )

    rows = -(-h_dim // chunk_h)
    cols = -(-w_dim // chunk_w)
    item_bytes = _DTYPE_BYTES[dtype_name]
    full = np.zeros(shape, dtype=np.dtype(dtype_name))
    for cr in range(rows):
        for cc in range(cols):
            chunk_key = f"c/0/{cr}/{cc}"
            raw = fetch_chunk(chunk_key)
            expected = chunk_c * chunk_h * chunk_w * item_bytes
            if len(raw) != expected:
                raise StrandError(
                    f"Chunk {chunk_key} has {len(raw)} bytes; expected {expected}",
                )
            buf = np.frombuffer(raw, dtype=np.dtype(dtype_name)).reshape(
                chunk_c, chunk_h, chunk_w
            )
            y0 = cr * chunk_h
            x0 = cc * chunk_w
            y1 = min(y0 + chunk_h, h_dim)
            x1 = min(x0 + chunk_w, w_dim)
            full[:, y0:y1, x0:x1] = buf[:, : (y1 - y0), : (x1 - x0)]
    return full


def _enumerate_chunks(array_meta: dict[str, Any]) -> Iterable[str]:
    shape = list(array_meta["shape"])
    chunks = list(array_meta["chunk_grid"]["configuration"]["chunk_shape"])
    if len(shape) != len(chunks):
        raise StrandError(f"Mismatched shape vs chunks: shape={shape}, chunks={chunks}")
    grid = [-(-s // c) for s, c in zip(shape, chunks, strict=True)]
    encoding = array_meta.get("chunk_key_encoding") or {}
    enc_cfg = encoding.get("configuration") or {}
    sep = enc_cfg.get("separator", "/") if isinstance(enc_cfg, dict) else "/"

    def _walk(prefix: list[int], dim: int) -> Iterable[str]:
        if dim == len(grid):
            return [sep.join(str(i) for i in prefix)]
        out: list[str] = []
        for i in range(grid[dim]):
            out.extend(_walk([*prefix, i], dim + 1))
        return out

    return [f"c/{key}" for key in _walk([], 0)]
