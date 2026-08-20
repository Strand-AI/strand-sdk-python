"""Static return-type contract for predict.submit overloads."""

from __future__ import annotations

from pathlib import Path

from mypy import api as mypy_api


def test_predict_submit_overloads_resolve_literal_dry_run(tmp_path: Path) -> None:
    source = tmp_path / "submit_types.py"
    source.write_text(
        """\
from typing import assert_type
from strand import Client, Estimate, Job

client: Client
assert_type(client.predict.submit("sample", ["CD3"]), Job)
assert_type(client.predict.submit("sample", ["CD3"], dry_run=False), Job)
assert_type(client.predict.submit("sample", ["CD3"], dry_run=True), Estimate)
flag: bool
assert_type(client.predict.submit("sample", ["CD3"], dry_run=flag), Job | Estimate)
"""
    )

    stdout, stderr, status = mypy_api.run(["--strict", str(source)])

    assert status == 0, stdout + stderr
