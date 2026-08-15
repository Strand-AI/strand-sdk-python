"""`client.markers` — request shape, parsing, and the 403 entitlement error."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

import strand


@respx.mock
def test_list_parses_entitlement_scoped_catalog(
    client: strand.Client, api_root: str
) -> None:
    route = respx.get(f"{api_root}/markers").mock(
        return_value=Response(
            200,
            json={
                "fullPanel": False,
                "count": 2,
                "markers": [
                    {"name": "DAPI", "publicPanel": True},
                    {"name": "CD8", "publicPanel": True},
                ],
            },
        )
    )
    catalog = client.markers.list()
    assert route.called
    assert catalog.full_panel is False
    assert catalog.names == ["DAPI", "CD8"]
    assert len(catalog) == 2
    assert [m.public_panel for m in catalog] == [True, True]


@respx.mock
def test_predict_maps_403_to_marker_not_available(
    client: strand.Client, api_root: str
) -> None:
    respx.post(f"{api_root}/predict").mock(
        return_value=Response(
            403,
            json={
                "error": "marker_not_available",
                "message": (
                    "Marker not available on this account: CD4. Additional markers are "
                    "available to contracted partners under agreement. Contact support@strandai.com."
                ),
                "unavailableMarkers": ["CD4"],
                "availableMarkers": ["DAPI", "CD8"],
            },
        )
    )
    with pytest.raises(strand.MarkerNotAvailableError) as excinfo:
        client.predict.submit("00000000-0000-4000-8000-0000000000ee", ["CD4"])
    err = excinfo.value
    assert err.status_code == 403
    assert err.unavailable == ["CD4"]
    assert err.available == ["DAPI", "CD8"]
