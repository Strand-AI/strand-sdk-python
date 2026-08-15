"""SDK <-> OpenAPI parity — the spec is the machine-checked hub (#672 phase 0).

`openapi.json` (kept canonical by the OpenAPI-Drift CI gate) is the source of
truth for the API surface. This test proves, in both directions, that the
Python SDK's public surface matches it:

  1. Every spec operation is either mapped to an SDK callable in
     ``SPEC_COVERAGE`` or allowlisted in ``UNMAPPED_OPERATIONS`` with a reason.
  2. Every spec field (query/path param + request-body property) of a mapped
     operation is either bound to a real parameter of that callable, marked
     ``DERIVED``/``BOUND``, or allowlisted in ``SPEC_FIELDS_NOT_IN_SDK``.
  3. Reverse: every parameter of every mapped callable is either the target of
     a spec-field binding or declared client-only in ``CLIENT_ONLY_PARAMS``.
  4. Stale entries anywhere (map keys not in the spec, allowlist entries that
     no longer apply, bindings to parameters that no longer exist) fail too.

Net effect: adding an API param without the SDK (the #667 `mpp` case), adding
an SDK param without the API, or adding an endpoint without a conscious
decision here all fail CI with instructions.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from strand._client import _JobsNamespace
from strand._jobs import Job
from strand._markers import Markers
from strand._predict import Predict
from strand._public import PublicSample, PublicSamples
from strand._results import JobResults
from strand._samples import Samples
from strand._uploads import Uploads

from .spec_parity_allowlist import (
    CLIENT_ONLY_PARAMS,
    SPEC_FIELDS_NOT_IN_SDK,
    UNMAPPED_OPERATIONS,
)

SPEC_PATH = Path(__file__).resolve().parents[1] / "openapi.json"

# Sentinels for spec fields with no direct keyword argument.
BOUND = "<bound>"  # supplied by the handle the method lives on (e.g. Job.id)
DERIVED = "<derived>"  # computed by the SDK from other inputs (e.g. fileSize)

# "METHOD /path" -> (representative SDK callable, {spec field -> SDK param}).
# The callable is the public way to reach the operation; the field map binds
# every spec-visible input by name.
SPEC_COVERAGE: dict[str, tuple[Any, dict[str, str]]] = {
    "GET /uploads": (Uploads.list, {"limit": "limit", "cursor": "cursor"}),
    "POST /uploads": (
        Uploads.upload_file,
        {
            "filename": "path",  # basename of the local file
            "fileSize": DERIVED,
            "contentType": "content_type",
            "contentSha256": DERIVED,
            "autoSegment": "auto_segment",
            "mpp": "mpp",
        },
    ),
    "GET /uploads/{id}": (Uploads.get, {"id": "upload_id"}),
    "POST /uploads/{id}/complete": (Uploads.upload_file, {"id": DERIVED}),
    "GET /markers": (Markers.list, {}),
    "POST /predict/estimate": (Predict.estimate, {"uploadId": "upload_id", "markers": "markers"}),
    "POST /predict": (
        Predict.submit,
        {"uploadId": "upload_id", "markers": "markers", "model": "model"},
    ),
    "GET /jobs/{id}": (_JobsNamespace.get, {"id": "job_id"}),
    "POST /jobs/{id}/cancel": (_JobsNamespace.cancel, {"id": "job_id"}),
    "GET /jobs/{id}/stream": (Job.stream_events, {"id": BOUND}),
    "GET /jobs/{id}/results": (Job.results, {"id": BOUND}),
    "GET /jobs/{id}/results/files/{path}": (
        JobResults.download_to,
        {"id": BOUND, "path": DERIVED},  # remote paths come from the zarr listing
    ),
    "POST /jobs/{id}/exports/ome-tiff": (Job.request_ome_tiff_export, {"id": BOUND}),
    "GET /jobs/{id}/exports/ome-tiff": (Job.get_ome_tiff_export, {"id": BOUND}),
    "POST /jobs/{id}/exports/ome-zarr-zip": (Job.request_results_archive, {"id": BOUND}),
    "GET /jobs/{id}/exports/ome-zarr-zip": (Job.get_results_archive, {"id": BOUND}),
    "GET /samples/{id}": (Samples.get, {"id": "sample_id"}),
    "PATCH /samples/{id}/mpp": (Samples.set_mpp, {"id": "sample_id", "mpp": "mpp"}),
    "PATCH /samples/{id}/expiration": (
        Samples.set_expiration,
        {
            "id": "sample_id",
            "expiresAt": "expires_at",
            "neverExpire": "never_expire",
            "useOrgDefault": "use_org_default",
            "reason": "reason",
        },
    ),
    "PATCH /samples/expiration": (
        Samples.set_expiration_bulk,
        {
            "sampleIds": "sample_ids",
            "expiresAt": "expires_at",
            "neverExpire": "never_expire",
            "useOrgDefault": "use_org_default",
            "reason": "reason",
        },
    ),
    "GET /public/samples": (
        PublicSamples.list,
        {"page": "page", "pageSize": "page_size", "tag": "tag"},
    ),
    "GET /public/samples/{publicId}": (PublicSamples.get, {"publicId": "public_id"}),
    "GET /public/samples/{publicId}/zarr/{path}": (
        PublicSample.download_to,
        {"publicId": BOUND, "path": DERIVED},  # id from the handle; paths from the zarr listing
    ),
    "POST /samples/{id}/restore": (Samples.restore, {"id": "sample_id"}),
    "GET /samples/{id}/tags": (Samples.list_tags, {"id": "sample_id"}),
    "POST /samples/{id}/tags": (Samples.add_tag, {"id": "sample_id", "tag": "tag"}),
    "DELETE /samples/{id}/tags": (Samples.remove_tag, {"id": "sample_id", "tag": "tag"}),
}

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _load_spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text())  # type: ignore[no-any-return]


def _resolve(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return components[schema["$ref"].rsplit("/", 1)[-1]]  # type: ignore[no-any-return]
    return schema


def _spec_fields(path_item: dict[str, Any], op: dict[str, Any], spec: dict[str, Any]) -> set[str]:
    """Names of every caller-supplied input: query/path params + body props."""
    components = spec.get("components", {}).get("schemas", {})
    fields = {
        p["name"]
        for p in list(path_item.get("parameters", [])) + list(op.get("parameters", []))
    }
    body = op.get("requestBody", {}).get("content", {}).get("application/json")
    if body is not None:
        schema = _resolve(body.get("schema", {}), components)
        for part in schema.get("allOf", [schema]):
            part = _resolve(part, components)
            fields |= set((part.get("properties") or {}).keys())
    return fields


def _spec_operations() -> dict[str, set[str]]:
    """{"METHOD /path": {field, ...}} for every operation in openapi.json."""
    spec = _load_spec()
    ops: dict[str, set[str]] = {}
    for path, path_item in spec["paths"].items():
        for method, op in path_item.items():
            if method in HTTP_METHODS:
                ops[f"{method.upper()} {path}"] = _spec_fields(path_item, op, spec)
    return ops


def _params_of(func: Any) -> set[str]:
    """Keyword-bindable parameter names, excluding self/*args/**kwargs."""
    return {
        name
        for name, p in inspect.signature(func).parameters.items()
        if name != "self"
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }


def test_every_spec_operation_is_covered_or_allowlisted() -> None:
    spec_ops = set(_spec_operations())
    mapped = set(SPEC_COVERAGE)
    allowlisted = set(UNMAPPED_OPERATIONS)

    assert not (mapped & allowlisted), (
        f"operations both mapped and allowlisted: {sorted(mapped & allowlisted)}"
    )
    missing = spec_ops - mapped - allowlisted
    assert not missing, (
        f"spec operations with no SDK surface: {sorted(missing)}. Add SDK support and map "
        "them in SPEC_COVERAGE, or allowlist them in UNMAPPED_OPERATIONS with a reason."
    )
    stale_map = mapped - spec_ops
    assert not stale_map, f"SPEC_COVERAGE entries not in openapi.json (stale): {sorted(stale_map)}"
    stale_allow = allowlisted - spec_ops
    assert not stale_allow, (
        f"UNMAPPED_OPERATIONS entries not in openapi.json (stale): {sorted(stale_allow)}"
    )


def test_every_spec_field_maps_to_an_sdk_parameter() -> None:
    spec_ops = _spec_operations()
    problems: list[str] = []
    for op_key, (func, field_map) in SPEC_COVERAGE.items():
        if op_key not in spec_ops:
            continue  # stale keys reported by the coverage test
        spec_fields = spec_ops[op_key]
        allow = SPEC_FIELDS_NOT_IN_SDK.get(op_key, {})
        params = _params_of(func)

        for field in sorted(spec_fields - set(field_map) - set(allow)):
            problems.append(
                f"{op_key}: spec field '{field}' is not reachable from the SDK. Add a "
                f"parameter to {func.__qualname__} and bind it in SPEC_COVERAGE, or "
                "allowlist it in SPEC_FIELDS_NOT_IN_SDK with a reason."
            )
        for field in sorted(set(field_map) - spec_fields):
            problems.append(f"{op_key}: SPEC_COVERAGE binds '{field}' which is not in the spec.")
        for field in sorted(set(allow) - spec_fields):
            problems.append(
                f"{op_key}: SPEC_FIELDS_NOT_IN_SDK lists '{field}' which is not in the spec "
                "(stale allowlist entry)."
            )
        for field in sorted(set(allow) & set(field_map)):
            problems.append(f"{op_key}: '{field}' is both bound and allowlisted — pick one.")
        for field, target in sorted(field_map.items()):
            if target not in (BOUND, DERIVED) and target not in params:
                problems.append(
                    f"{op_key}: '{field}' is bound to parameter '{target}' which does not "
                    f"exist on {func.__qualname__} (has: {sorted(params)})."
                )
    assert not problems, "\n".join(problems)


def test_every_sdk_parameter_exists_in_the_spec() -> None:
    """Reverse direction: SDK params that the spec doesn't know about fail."""
    targets_by_func: dict[str, set[str]] = {}
    funcs: dict[str, Any] = {}
    for _op_key, (func, field_map) in SPEC_COVERAGE.items():
        qualname = func.__qualname__
        funcs[qualname] = func
        targets_by_func.setdefault(qualname, set()).update(
            t for t in field_map.values() if t not in (BOUND, DERIVED)
        )

    problems: list[str] = []
    for qualname, func in sorted(funcs.items()):
        client_only = CLIENT_ONLY_PARAMS.get(qualname, {})
        params = _params_of(func)
        unexplained = params - targets_by_func[qualname] - set(client_only)
        for param in sorted(unexplained):
            problems.append(
                f"{qualname}: parameter '{param}' has no spec counterpart. Bind it to a "
                "spec field in SPEC_COVERAGE, or declare it in CLIENT_ONLY_PARAMS with a "
                "reason."
            )
        for param in sorted(set(client_only) - params):
            problems.append(
                f"{qualname}: CLIENT_ONLY_PARAMS lists '{param}' which is no longer a "
                "parameter (stale allowlist entry)."
            )
        for param in sorted(set(client_only) & targets_by_func[qualname]):
            problems.append(
                f"{qualname}: '{param}' is both spec-bound and client-only — pick one."
            )
    for qualname in sorted(set(CLIENT_ONLY_PARAMS) - set(funcs)):
        problems.append(
            f"CLIENT_ONLY_PARAMS has entries for '{qualname}' which is not a mapped callable."
        )
    assert not problems, "\n".join(problems)


def test_allowlists_have_reasons() -> None:
    entries: list[tuple[str, str]] = list(UNMAPPED_OPERATIONS.items())
    for op_key, fields in SPEC_FIELDS_NOT_IN_SDK.items():
        entries += [(f"{op_key} {f}", reason) for f, reason in fields.items()]
    for qualname, params in CLIENT_ONLY_PARAMS.items():
        entries += [(f"{qualname} {p}", reason) for p, reason in params.items()]
    unreasoned = [key for key, reason in entries if not reason.strip()]
    assert not unreasoned, f"allowlist entries without a reason: {unreasoned}"
