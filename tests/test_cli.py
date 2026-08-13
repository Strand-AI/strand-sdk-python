"""CLI tests: arg parsing + that each command calls the right client method.

The CLI is a thin wrapper over `strand.Client`; these tests mock the client
(so no network) and assert the mapping from command-line args to SDK calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from strand import _cli
from strand._errors import AuthError
from strand._models import JobStatus, Upload
from strand._uploads import UploadList

runner = CliRunner()


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch `_cli.Client` so every command runs against a MagicMock."""
    client = MagicMock()
    monkeypatch.setattr(_cli, "Client", lambda *a, **k: client)
    return client


@pytest.fixture
def slide(tmp_path: Path) -> Path:
    f = tmp_path / "slide.svs"
    f.write_bytes(b"x" * 16)
    return f


# ---------- upload ----------


def test_upload_defaults(fake_client: MagicMock, slide: Path) -> None:
    fake_client.uploads.upload_file.return_value = Upload(
        id="u1", width_px=10, height_px=20, status="ready"
    )
    result = runner.invoke(_cli.app, ["upload", str(slide)])
    assert result.exit_code == 0, result.output
    fake_client.uploads.upload_file.assert_called_once()
    args, kwargs = fake_client.uploads.upload_file.call_args
    assert args[0] == str(slide)
    assert kwargs["content_type"] is None
    assert kwargs["if_not_exists"] is False
    assert '"id": "u1"' in result.output
    fake_client.close.assert_called_once()


def test_upload_options(fake_client: MagicMock, slide: Path) -> None:
    fake_client.uploads.upload_file.return_value = Upload(id="u1")
    result = runner.invoke(
        _cli.app,
        ["upload", str(slide), "--content-type", "image/tiff", "--if-not-exists"],
    )
    assert result.exit_code == 0, result.output
    _, kwargs = fake_client.uploads.upload_file.call_args
    assert kwargs["content_type"] == "image/tiff"
    assert kwargs["if_not_exists"] is True


def test_upload_mpp(fake_client: MagicMock, slide: Path) -> None:
    fake_client.uploads.upload_file.return_value = Upload(id="u1")
    result = runner.invoke(_cli.app, ["upload", str(slide), "--mpp", "0.25"])
    assert result.exit_code == 0, result.output
    _, kwargs = fake_client.uploads.upload_file.call_args
    assert kwargs["mpp"] == 0.25


def test_upload_missing_file_errors(fake_client: MagicMock, tmp_path: Path) -> None:
    result = runner.invoke(_cli.app, ["upload", str(tmp_path / "nope.svs")])
    assert result.exit_code != 0
    fake_client.uploads.upload_file.assert_not_called()


# ---------- predict ----------


def test_predict_parses_markers(fake_client: MagicMock) -> None:
    job = MagicMock()
    job.id = "job1"
    job.reserved_credits = 42
    fake_client.predict.submit.return_value = job
    result = runner.invoke(
        _cli.app, ["predict", "u1", "--markers", "CD3e, CD8 ,PanCK"]
    )
    assert result.exit_code == 0, result.output
    args, kwargs = fake_client.predict.submit.call_args
    assert args[0] == "u1"
    assert args[1] == ["CD3e", "CD8", "PanCK"]
    assert kwargs["model"] is None
    assert '"job_id": "job1"' in result.output
    assert '"reserved_credits": 42' in result.output


def test_predict_model_flag(fake_client: MagicMock) -> None:
    job = MagicMock()
    job.id = "job1"
    job.reserved_credits = 1
    fake_client.predict.submit.return_value = job
    result = runner.invoke(_cli.app, ["predict", "u1", "-m", "CD8", "--model", "v0.4"])
    assert result.exit_code == 0, result.output
    _, kwargs = fake_client.predict.submit.call_args
    assert kwargs["model"] == "v0.4"


def test_predict_rejects_empty_markers(fake_client: MagicMock) -> None:
    result = runner.invoke(_cli.app, ["predict", "u1", "--markers", " , "])
    assert result.exit_code != 0
    fake_client.predict.submit.assert_not_called()


# ---------- status ----------


def test_status_fetches_job(fake_client: MagicMock) -> None:
    snapshot = JobStatus(
        id="job1",
        status="completed",
        progress=1.0,
        reserved_credits=5,
        markers=["CD8"],
        created_at=None,
        started_at=None,
        completed_at=None,
        error_message=None,
        results_available=True,
        model="v0.5",
    )
    job = MagicMock()
    job.status = snapshot
    fake_client.jobs.get.return_value = job
    result = runner.invoke(_cli.app, ["status", "job1"])
    assert result.exit_code == 0, result.output
    fake_client.jobs.get.assert_called_once_with("job1")
    assert '"status": "completed"' in result.output


# ---------- results ----------


def test_results_downloads_to_out(fake_client: MagicMock, tmp_path: Path) -> None:
    out = tmp_path / "res"
    job = MagicMock()
    job.download_results.return_value = out
    fake_client.jobs.get.return_value = job
    result = runner.invoke(_cli.app, ["results", "job1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    fake_client.jobs.get.assert_called_once_with("job1")
    job.download_results.assert_called_once_with(str(out))
    assert str(out) in result.output


def test_results_default_out(fake_client: MagicMock) -> None:
    job = MagicMock()
    job.download_results.return_value = Path("results")
    fake_client.jobs.get.return_value = job
    result = runner.invoke(_cli.app, ["results", "job1"])
    assert result.exit_code == 0, result.output
    job.download_results.assert_called_once_with("results")


# ---------- samples list ----------


def test_samples_list(fake_client: MagicMock) -> None:
    page = UploadList(
        uploads=[Upload(id="u1", filename="a.svs", status="ready")],
        next_cursor="c2",
    )
    fake_client.uploads.list.return_value = page
    result = runner.invoke(_cli.app, ["samples", "list", "--limit", "50"])
    assert result.exit_code == 0, result.output
    fake_client.uploads.list.assert_called_once_with(limit=50, cursor=None)
    assert '"id": "u1"' in result.output
    assert '"next_cursor": "c2"' in result.output


# ---------- auth / error paths ----------


def test_missing_api_key_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> MagicMock:
        raise AuthError(
            "No API key provided. Pass api_key=... or set STRAND_API_KEY.",
            status_code=401,
            error_code="missing_api_key",
        )

    monkeypatch.setattr(_cli, "Client", boom)
    result = runner.invoke(_cli.app, ["samples", "list"])
    assert result.exit_code == 1


def test_client_error_exits_nonzero(fake_client: MagicMock) -> None:
    fake_client.jobs.get.side_effect = AuthError("bad key", status_code=401)
    result = runner.invoke(_cli.app, ["status", "job1"])
    assert result.exit_code == 1
    fake_client.close.assert_called_once()


# ---------- help renders ----------


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["upload", "--help"],
        ["predict", "--help"],
        ["status", "--help"],
        ["results", "--help"],
        ["samples", "--help"],
        ["samples", "list", "--help"],
    ],
)
def test_help_renders(argv: list[str]) -> None:
    result = runner.invoke(_cli.app, argv)
    assert result.exit_code == 0, result.output
    assert result.output
