"""Markers namespace — the entitlement-scoped catalog of predictable markers.

Exposed as `client.markers`. Mirrors:

    GET /api/v1/markers  -> list()

Credit-free. The returned list is exactly what `client.predict` and
`client.predict.submit` will accept for this account: a self-signup account
sees the public panel; a full-panel account sees the whole vocab. Use it to
discover valid marker names upfront instead of trial-and-error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._models import MarkerList

if TYPE_CHECKING:
    from ._http import HttpSession


class Markers:
    def __init__(self, http: HttpSession) -> None:
        self._http = http

    def list(self) -> MarkerList:
        """Return the markers this account may request (credit-free)."""
        raw = self._http.request_json("GET", "/markers")
        return MarkerList._from_dict(raw)
