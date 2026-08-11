"""Conscious-decision allowlists for the SDK <-> OpenAPI parity test.

Every entry is an INTENTIONAL gap between `openapi.json` and the Python SDK
surface, with a one-line reason. `test_spec_parity.py` fails on any gap that
is not recorded here, and fails on stale entries the moment the gap closes —
so this file is always an accurate inventory of what we deliberately skip.

Adding an entry requires a reason; "TODO" reasons are welcome for real gaps
we intend to close (they keep CI green while staying visible in review).
"""

from __future__ import annotations

# Spec operations ("METHOD /path") with NO Python SDK surface at all.
# Empty today: every operation in openapi.json is reachable from `strand`.
UNMAPPED_OPERATIONS: dict[str, str] = {}

# Per-operation spec fields (query/path params + request-body properties)
# deliberately NOT settable through the SDK. Keyed "METHOD /path" -> field ->
# reason. Empty today: every spec field maps to an SDK parameter or is derived
# internally.
SPEC_FIELDS_NOT_IN_SDK: dict[str, dict[str, str]] = {}

# SDK parameters that intentionally have NO spec counterpart because they are
# client-side ergonomics, keyed by the callable's qualname. The parity test
# fails if a mapped callable grows a parameter that is neither mapped to a
# spec field nor listed here — adding an SDK knob is a conscious decision too.
CLIENT_ONLY_PARAMS: dict[str, dict[str, str]] = {
    "Uploads.upload_file": {
        "chunk_size": "GCS resumable-upload chunk size; never leaves the client",
        "progress": "local progress callback for the byte stream",
        "if_not_exists": "toggles client-side sha256 dedupe (sends contentSha256 or not)",
    },
    "JobResults.download_to": {
        "target": "local destination directory for the fetched zarr tree",
    },
}
