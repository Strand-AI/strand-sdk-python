"""Results download + AnnData conversion.

The platform writes an OME-Zarr v3 group at `<resultBasePath>`. The root group
holds one `multiscales` entry per modality (the source H&E image plus one per
predicted marker), each pointing at its own pyramid:

    zarr.json                     root group (omero/multiscales metadata)
    he/{level}/zarr.json          H&E source array per pyramid level
    he/{level}/c/0/{cr}/{cc}      chunk or shard bytes
    markers/{name}/{level}/...    one [1, H, W] array per marker (per level)

Storage objects under `c/` are addressed by the array's `chunk_grid`, which for
current results is the *shard* grid: markers and H&E are written with the zarr
v3 `sharding_indexed` codec, packing 8x8 inner 256x256 chunks into one object
(cuts GCS object count ~64x — see `platform/inference/pyramid.py`). Inner chunks
are individually zstd-compressed. Older results are unsharded raw
little-endian bytes; both layouts are read here.

The SDK decodes zarr directly with numpy — no dependency on zarr-python. That
keeps the base install to `httpx`, and the store is only reachable through the
API-key-authenticated proxy at `/api/v1/jobs/{id}/results/files/{path}`, which
zarr-python could not read without a custom Store implementation anyway.
Decompression needs a zstd binding: Python 3.14's stdlib `compression.zstd` is
used when available, otherwise the `zstandard` package (pulled in by the
`anndata` extra alongside numpy).
"""

from __future__ import annotations

import copy
import json
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._errors import NotFoundError, StrandError

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

#: zarr v3 sharding "inner chunk not present" sentinel: offset == nbytes == 2**64 - 1.
_SHARD_EMPTY = (1 << 64) - 1


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

        The store, not the manifest, is authoritative. A dataset the root
        manifest declares but storage doesn't hold is dropped from the local
        copy's `zarr.json` and a `UserWarning` names it, so the mirror stays a
        valid zarr store that `zarr.open()` can read. (Results written before
        the level-count fix over-declare one pyramid level per marker; this is
        what makes them readable without a backfill.) A chunk object that is
        absent is left absent — zarr reads missing chunks as `fill_value`.
        """
        return mirror_zarr_store(self.root_meta(), self.get_json, self.get_bytes, target)

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
            # anndata's stub narrows obsm values to Sequence[Any], while a
            # NumPy ndarray is accepted at runtime but does not implement the
            # nominal Sequence protocol in current NumPy typing.
            obsm={"spatial": cast(Sequence[Any], spatial)},
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


def mirror_zarr_store(
    root_meta: dict[str, Any],
    get_json: Any,
    get_bytes: Any,
    target: str | Path,
) -> Path:
    """Byte-for-byte mirror an OME-Zarr v3 store to `target/`, returning it.

    The store, not the manifest, is authoritative: a dataset the root manifest
    declares but storage doesn't hold is dropped from the local copy's
    `zarr.json` (with a `UserWarning`), and an absent chunk object is left
    absent (zarr reads it as `fill_value`). Parameterized on `get_json(path)` /
    `get_bytes(path)` fetchers so it serves both the job-results proxy and the
    public-cohort zarr proxy — the two stores are the same shape.
    """
    out = Path(target)
    out.mkdir(parents=True, exist_ok=True)

    root = copy.deepcopy(root_meta)
    missing: list[str] = []

    for ms in _multiscales(root):
        kept: list[dict[str, Any]] = []
        for dataset in ms.get("datasets") or []:
            path = dataset.get("path")
            if not isinstance(path, str):
                continue
            try:
                array_meta = get_json(f"{path}/zarr.json")
            except NotFoundError:
                missing.append(path)
                continue
            kept.append(dataset)
            dest = out / path
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "zarr.json").write_bytes(json.dumps(array_meta).encode("utf-8"))
            for chunk_path in _enumerate_chunks(array_meta):
                file_path = f"{path}/{chunk_path}"
                try:
                    data = get_bytes(file_path)
                except NotFoundError:
                    # Absent chunk/shard object == every element is fill_value.
                    continue
                full = out / file_path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(data)
        ms["datasets"] = kept

    (out / "zarr.json").write_bytes(json.dumps(root).encode("utf-8"))

    if missing:
        warnings.warn(
            "Zarr manifest declares datasets that are not in storage; "
            f"omitted from the local copy: {', '.join(missing)}",
            UserWarning,
            stacklevel=2,
        )
    return out


def _zstd_decompress(data: bytes, expected: int) -> bytes:
    """Inflate one zstd frame whose decompressed size is known to be `expected`.

    Prefers Python 3.14's stdlib binding, then the `zstandard` package. The
    size is passed explicitly rather than trusting the frame header, which is
    optional in the zstd format.
    """
    try:
        from compression.zstd import (  # type: ignore[import-not-found]
            decompress as _stdlib_decompress,
        )
    except ImportError:
        pass
    else:
        return cast(bytes, _stdlib_decompress(data))

    try:
        import zstandard
    except ImportError as exc:
        raise StrandError(
            "Reading results requires a zstd decoder (result chunks are "
            "zstd-compressed). Install with: pip install 'strand-sdk[anndata]' "
            "(or 'pip install zstandard').",
        ) from exc
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=expected)


def _codec_names(codecs: Any) -> list[str]:
    if not isinstance(codecs, list):
        return []
    return [str(c.get("name")) for c in codecs if isinstance(c, dict)]


def _bytes_codec_ok(codecs: Any) -> bool:
    """True when `codecs[0]` is a little-endian `bytes` codec."""
    if not isinstance(codecs, list) or not codecs or not isinstance(codecs[0], dict):
        return False
    if codecs[0].get("name") != "bytes":
        return False
    cfg = codecs[0].get("configuration") or {}
    return bool(cfg.get("endian", "little") == "little")


def _payload_uses_zstd(codecs: Any, where: str) -> bool:
    """Validate a `[bytes]` / `[bytes, zstd]` codec chain; return whether zstd is on."""
    names = _codec_names(codecs)
    if _bytes_codec_ok(codecs):
        if names == ["bytes"]:
            return False
        if names == ["bytes", "zstd"]:
            return True
    raise StrandError(
        f"Unsupported {where} codec chain {codecs!r}; the SDK reads "
        "['bytes'] and ['bytes', 'zstd'].",
    )


@dataclass(frozen=True, slots=True)
class _Sharding:
    """Parsed `sharding_indexed` configuration for one array."""

    inner_chunk: tuple[int, int, int]
    #: inner chunks per shard, per axis — e.g. `(1, 8, 8)`.
    per_shard: tuple[int, int, int]
    payload_zstd: bool
    index_location: str
    #: Bytes the index occupies in the shard, including any checksum.
    index_nbytes: int
    slots: int


def _parse_sharding(codec: dict[str, Any], shard_shape: list[int]) -> _Sharding:
    cfg = codec.get("configuration") or {}
    inner = cfg.get("chunk_shape")
    if not isinstance(inner, list) or len(inner) != len(shard_shape):
        raise StrandError(f"Bad sharding_indexed chunk_shape {inner!r}")
    inner_t = tuple(int(v) for v in inner)
    per_shard = tuple(-(-int(s) // int(i)) for s, i in zip(shard_shape, inner_t, strict=True))
    if per_shard[0] != 1:
        raise StrandError(
            "SDK expects all channels in a single inner chunk along C; "
            f"got inner chunk {inner_t} in shard {shard_shape}.",
        )

    payload_zstd = _payload_uses_zstd(cfg.get("codecs"), "sharding payload")

    index_codecs = cfg.get("index_codecs")
    index_names = _codec_names(index_codecs)
    if not _bytes_codec_ok(index_codecs) or index_names not in (["bytes"], ["bytes", "crc32c"]):
        raise StrandError(
            f"Unsupported shard index codec chain {index_codecs!r}; the SDK reads "
            "['bytes'] and ['bytes', 'crc32c'].",
        )

    slots = per_shard[0] * per_shard[1] * per_shard[2]
    # 16 bytes per slot (offset u64 LE + nbytes u64 LE), plus crc32c's trailing
    # u32 when declared. The checksum is skipped, not verified — the transport
    # is HTTPS and verifying would cost a dependency for no added integrity.
    index_nbytes = slots * 16 + (4 if index_names == ["bytes", "crc32c"] else 0)
    location = cfg.get("index_location", "end")
    if location not in ("start", "end"):
        raise StrandError(f"Unsupported shard index_location {location!r}")

    return _Sharding(
        inner_chunk=cast(tuple[int, int, int], inner_t),
        per_shard=cast(tuple[int, int, int], per_shard),
        payload_zstd=payload_zstd,
        index_location=location,
        index_nbytes=index_nbytes,
        slots=slots,
    )


def _shard_index(raw: bytes, sharding: _Sharding, key: str) -> list[tuple[int, int]]:
    """Slice a shard's index out of the shard blob → one `(offset, nbytes)` per slot."""
    if len(raw) < sharding.index_nbytes:
        raise StrandError(
            f"Shard {key} is {len(raw)} bytes; too short for a "
            f"{sharding.index_nbytes}-byte index",
        )
    body = (
        raw[: sharding.index_nbytes]
        if sharding.index_location == "start"
        else raw[len(raw) - sharding.index_nbytes :]
    )
    return [
        (
            int.from_bytes(body[i * 16 : i * 16 + 8], "little"),
            int.from_bytes(body[i * 16 + 8 : i * 16 + 16], "little"),
        )
        for i in range(sharding.slots)
    ]


def _read_array(meta: dict[str, Any], fetch_chunk: Any, np: Any) -> Any:
    """Decode one zarr v3 array into a numpy `[C, H, W]`.

    `fetch_chunk(key)` returns the bytes of the storage object at `key`
    (e.g. `"c/0/3/4"`) or raises `NotFoundError` when it is absent — an absent
    object means every element it covers is `fill_value`.
    """
    shape = [int(v) for v in meta["shape"]]
    grid_chunk = [int(v) for v in meta["chunk_grid"]["configuration"]["chunk_shape"]]
    dtype_name = str(meta["data_type"])
    if dtype_name not in _DTYPE_BYTES:
        raise StrandError(f"Unsupported dtype in zarr: {dtype_name!r}")
    if len(shape) != 3:
        raise StrandError(f"Expected 3-dim [C, H, W] array, got shape {shape}")

    codecs = meta.get("codecs") or []
    sharding: _Sharding | None = None
    shard_codec = next(
        (c for c in codecs if isinstance(c, dict) and c.get("name") == "sharding_indexed"),
        None,
    )
    if shard_codec is not None:
        if len(codecs) != 1:
            raise StrandError(
                f"Unsupported codec chain around sharding_indexed: {codecs!r}",
            )
        sharding = _parse_sharding(shard_codec, grid_chunk)
        payload_zstd = sharding.payload_zstd
        inner = list(sharding.inner_chunk)
    else:
        payload_zstd = _payload_uses_zstd(codecs, "array")
        inner = grid_chunk

    c_dim, h_dim, w_dim = shape
    inner_c, inner_h, inner_w = inner
    if inner_c != c_dim:
        raise StrandError(
            "SDK currently expects all channels in a single chunk along C; "
            f"got chunks={inner}, shape={shape}.",
        )

    dtype = np.dtype(dtype_name)
    inner_bytes = inner_c * inner_h * inner_w * _DTYPE_BYTES[dtype_name]
    fill = meta.get("fill_value", 0)
    full = np.full(shape, fill, dtype=dtype)

    def place(cr: int, cc: int, raw: bytes, key: str) -> None:
        if len(raw) != inner_bytes:
            raise StrandError(f"Chunk {key} has {len(raw)} bytes; expected {inner_bytes}")
        buf = np.frombuffer(raw, dtype=dtype).reshape(inner_c, inner_h, inner_w)
        y0, x0 = cr * inner_h, cc * inner_w
        y1, x1 = min(y0 + inner_h, h_dim), min(x0 + inner_w, w_dim)
        if y1 > y0 and x1 > x0:
            full[:, y0:y1, x0:x1] = buf[:, : (y1 - y0), : (x1 - x0)]

    # Storage objects are addressed by the *outer* grid: shards when sharded,
    # chunks otherwise.
    grid_h, grid_w = grid_chunk[1], grid_chunk[2]
    for gr in range(-(-h_dim // grid_h)):
        for gc in range(-(-w_dim // grid_w)):
            key = f"c/0/{gr}/{gc}"
            try:
                raw = fetch_chunk(key)
            except NotFoundError:
                continue
            if sharding is None:
                place(gr, gc, _zstd_decompress(raw, inner_bytes) if payload_zstd else raw, key)
                continue
            index = _shard_index(raw, sharding, key)
            for iy in range(sharding.per_shard[1]):
                for ix in range(sharding.per_shard[2]):
                    offset, nbytes = index[iy * sharding.per_shard[2] + ix]
                    if offset == _SHARD_EMPTY or nbytes == _SHARD_EMPTY:
                        continue
                    if offset + nbytes > len(raw):
                        raise StrandError(
                            f"Shard {key} index points past end of object "
                            f"({offset}+{nbytes} > {len(raw)})",
                        )
                    payload = raw[offset : offset + nbytes]
                    place(
                        gr * sharding.per_shard[1] + iy,
                        gc * sharding.per_shard[2] + ix,
                        _zstd_decompress(payload, inner_bytes) if payload_zstd else payload,
                        f"{key}[{iy},{ix}]",
                    )
    return full


def _enumerate_chunks(array_meta: dict[str, Any]) -> Iterable[str]:
    """Every storage object key under `c/` for one array.

    Driven by `chunk_grid`, which is the *shard* grid on sharded arrays — so
    this enumerates one key per stored object either way, which is what a
    byte-for-byte mirror needs.
    """
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
