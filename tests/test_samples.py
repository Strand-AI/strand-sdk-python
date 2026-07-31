"""Samples namespace request-shape and validation tests."""

from __future__ import annotations

import math

import pytest
import respx
from httpx import Response

import strand


@respx.mock
def test_set_mpp_sends_isotropic_scalar(client: strand.Client, api_root: str) -> None:
    route = respx.patch(f"{api_root}/samples/sample-1/mpp").mock(
        return_value=Response(
            200,
            json={"id": "sample-1", "userMpp": {"x": 0.26, "y": 0.26}},
        )
    )

    result = client.samples.set_mpp("sample-1", 0.26)

    assert route.calls.last.request.content == b'{"mpp":0.26}'
    assert result == {"id": "sample-1", "userMpp": {"x": 0.26, "y": 0.26}}


@respx.mock
def test_set_mpp_sends_both_axes(client: strand.Client, api_root: str) -> None:
    route = respx.patch(f"{api_root}/samples/sample-1/mpp").mock(
        return_value=Response(
            200,
            json={"id": "sample-1", "userMpp": {"x": 0.26, "y": 0.25}},
        )
    )

    client.samples.set_mpp("sample-1", 0.26, 0.25)

    assert route.calls.last.request.content == b'{"mpp":{"x":0.26,"y":0.25}}'


@pytest.mark.parametrize("value", [0, -0.1, 100.1, math.inf, math.nan, True, "0.5"])
def test_set_mpp_rejects_invalid_values(client: strand.Client, value: object) -> None:
    with pytest.raises(ValueError, match="greater than 0 and at most 100"):
        client.samples.set_mpp("sample-1", value)  # type: ignore[arg-type]
