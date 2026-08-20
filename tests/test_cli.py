"""CLI tests: arg parsing + that each command calls the right client method.

The CLI is a thin wrapper over `strand.Client`; these tests mock the client
(so no network) and assert the mapping from command-line args to SDK calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from strand import _cli
from strand._errors import AuthError
from strand._jobs import Job
from strand._models import Estimate, JobStatus, PublicSampleGeometry, Upload
from strand._public import PublicSample
from strand._uploads import UploadList

runner = CliRunner()
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


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
    assert kwargs["dry_run"] is False
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


def test_predict_dry_run_prints_full_estimate_without_job_fields(fake_client: MagicMock) -> None:
    fake_client.predict.submit.return_value = Estimate(
        patch_count=101,
        marker_count=3,
        estimated_credits=303,
        org_balance=10_000,
        org_pending=500,
    )

    result = runner.invoke(
        _cli.app, ["predict", "u1", "--markers", "CD3e,CD8,PanCK", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    fake_client.predict.submit.assert_called_once_with(
        "u1", ["CD3e", "CD8", "PanCK"], model=None, dry_run=True
    )
    assert '"patch_count": 101' in result.output
    assert '"marker_count": 3' in result.output
    assert '"estimated_credits": 303' in result.output
    assert '"org_balance": 10000' in result.output
    assert '"org_pending": 500' in result.output
    assert "job_id" not in result.output
    assert "reserved_credits" not in result.output


def test_predict_dry_run_surfaces_errors(fake_client: MagicMock) -> None:
    fake_client.predict.submit.side_effect = AuthError("bad key", status_code=401)

    result = runner.invoke(_cli.app, ["predict", "u1", "-m", "CD8", "--dry-run"])

    assert result.exit_code == 1
    assert "error: bad key" in result.output
    fake_client.close.assert_called_once()


def test_predict_rejects_empty_markers(fake_client: MagicMock) -> None:
    result = runner.invoke(_cli.app, ["predict", "u1", "--markers", " , "])
    assert result.exit_code != 0
    fake_client.predict.submit.assert_not_called()


# ---------- status / wait / cancel ----------


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


def _status(state: str) -> JobStatus:
    return JobStatus(
        id="job1",
        status=state,
        progress=1.0,
        reserved_credits=5,
        markers=["CD8"],
        created_at=None,
        started_at=None,
        completed_at=None,
        error_message=None,
        results_available=state in {"completed", "partial_failed"},
        model="v0.5",
    )


def test_wait_wraps_job_wait_and_prints_terminal_snapshot(fake_client: MagicMock) -> None:
    job = MagicMock()
    job.wait.return_value = _status("partial_failed")
    fake_client.jobs.get.return_value = job

    result = runner.invoke(
        _cli.app,
        ["wait", "job1", "--timeout", "45", "--poll-interval", "0.25"],
    )

    assert result.exit_code == 0, result.output
    fake_client.jobs.get.assert_called_once_with("job1")
    job.wait.assert_called_once_with(timeout=45.0, poll_interval=0.25)
    assert '"status": "partial_failed"' in result.output
    assert '"results_available": true' in result.output


def test_wait_defaults_to_unbounded_sse_with_two_second_poll_fallback(
    fake_client: MagicMock,
) -> None:
    job = MagicMock()
    job.wait.return_value = _status("completed")
    fake_client.jobs.get.return_value = job

    result = runner.invoke(_cli.app, ["wait", "job1"])

    assert result.exit_code == 0, result.output
    job.wait.assert_called_once_with(timeout=None, poll_interval=2.0)


def test_wait_surfaces_errors(fake_client: MagicMock) -> None:
    job = MagicMock()
    job.wait.side_effect = AuthError("wait denied", status_code=403)
    fake_client.jobs.get.return_value = job

    result = runner.invoke(_cli.app, ["wait", "job1", "--timeout", "1"])

    assert result.exit_code == 1
    assert "error: wait denied" in result.output
    fake_client.close.assert_called_once()


def test_cancel_wraps_job_cancel_and_prints_post_cancel_snapshot(
    fake_client: MagicMock,
) -> None:
    job = MagicMock()
    job.cancel.return_value = _status("cancelled")
    fake_client.jobs.get.return_value = job

    result = runner.invoke(_cli.app, ["cancel", "job1"])

    assert result.exit_code == 0, result.output
    fake_client.jobs.get.assert_called_once_with("job1")
    job.cancel.assert_called_once_with()
    assert '"status": "cancelled"' in result.output


def test_cancel_surfaces_errors(fake_client: MagicMock) -> None:
    job = MagicMock()
    job.cancel.side_effect = AuthError("cannot cancel", status_code=400)
    fake_client.jobs.get.return_value = job

    result = runner.invoke(_cli.app, ["cancel", "job1"])

    assert result.exit_code == 1
    assert "error: cannot cancel" in result.output
    fake_client.close.assert_called_once()


# ---------- results / OME-TIFF ----------


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


def test_ome_tiff_wraps_export_and_prints_written_path(
    fake_client: MagicMock, tmp_path: Path
) -> None:
    out = tmp_path / "exports" / "job1.ome.tiff"
    job = MagicMock()
    job.export_ome_tiff.return_value = out
    fake_client.jobs.get.return_value = job

    result = runner.invoke(
        _cli.app,
        [
            "ome-tiff",
            "job1",
            "--out",
            str(out),
            "--timeout",
            "90",
            "--poll-interval",
            "0.5",
        ],
    )

    assert result.exit_code == 0, result.output
    fake_client.jobs.get.assert_called_once_with("job1")
    job.export_ome_tiff.assert_called_once_with(
        str(out), timeout=90.0, poll_interval=0.5
    )
    assert json.loads(result.output) == {"path": str(out)}


def test_ome_tiff_requires_out(fake_client: MagicMock) -> None:
    result = runner.invoke(_cli.app, ["ome-tiff", "job1"])

    assert result.exit_code != 0
    assert "--out" in _plain(result.output)
    fake_client.jobs.get.assert_not_called()


def test_ome_tiff_surfaces_errors(fake_client: MagicMock, tmp_path: Path) -> None:
    job = MagicMock()
    job.export_ome_tiff.side_effect = AuthError("export denied", status_code=403)
    fake_client.jobs.get.return_value = job

    result = runner.invoke(
        _cli.app, ["ome-tiff", "job1", "--out", str(tmp_path / "out.ome.tiff")]
    )

    assert result.exit_code == 1
    assert "error: export denied" in result.output
    fake_client.close.assert_called_once()


def test_ome_tiff_surfaces_terminal_poll_failure_from_real_job(
    fake_client: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_client = MagicMock()
    job_client._http.request_json.side_effect = [
        {
            "status": "running",
            "format": "ome-tiff",
            "sizeBytes": None,
            "updatedAt": None,
        },
        {
            "status": "failed",
            "format": "ome-tiff",
            "sizeBytes": None,
            "error": "renderer crashed",
            "updatedAt": None,
        },
    ]
    fake_client.jobs.get.return_value = Job(
        id="job1", reserved_credits=None, client=job_client
    )
    monkeypatch.setattr("strand._jobs.time.sleep", lambda _seconds: None)

    result = runner.invoke(
        _cli.app,
        [
            "ome-tiff",
            "job1",
            "--out",
            str(tmp_path / "out.ome.tiff"),
            "--timeout",
            "90",
            "--poll-interval",
            "0.001",
        ],
    )

    assert result.exit_code == 1
    assert "error: renderer crashed" in result.output
    assert [call.args[:2] for call in job_client._http.request_json.call_args_list] == [
        ("POST", "/jobs/job1/exports/ome-tiff"),
        ("GET", "/jobs/job1/exports/ome-tiff"),
    ]


# ---------- samples ----------


def test_samples_list_defaults_to_exact_four_options(fake_client: MagicMock) -> None:
    fake_client.samples.list.return_value = {"items": [], "next_cursor": None}

    result = runner.invoke(_cli.app, ["samples", "list"])

    assert result.exit_code == 0, result.output
    fake_client.samples.list.assert_called_once_with(
        scope="mine", limit=48, cursor=None, tag=None
    )


def test_samples_list_passes_scope_limit_cursor_and_tag(fake_client: MagicMock) -> None:
    fake_client.samples.list.return_value = {"items": [], "next_cursor": "c2"}

    result = runner.invoke(
        _cli.app,
        [
            "samples",
            "list",
            "--scope",
            "all",
            "--limit",
            "17",
            "--cursor",
            "c1",
            "--tag",
            "trial",
        ],
    )

    assert result.exit_code == 0, result.output
    fake_client.samples.list.assert_called_once_with(
        scope="all", limit=17, cursor="c1", tag="trial"
    )
    assert '"next_cursor": "c2"' in result.output


def test_samples_get_prints_owned_branch_with_jobs(fake_client: MagicMock) -> None:
    fake_client.samples.get.return_value = {
        "ownership": "mine",
        "id": "sample-1",
        "jobs": [{"id": "job-1", "status": "partial_failed"}],
        "job_count": 1,
    }

    result = runner.invoke(_cli.app, ["samples", "get", "sample-1"])

    assert result.exit_code == 0, result.output
    fake_client.samples.get.assert_called_once_with("sample-1")
    assert '"ownership": "mine"' in result.output
    assert '"job_count": 1' in result.output


def _public_sample() -> PublicSample:
    return PublicSample(
        http=MagicMock(),
        id="pub-1",
        title="TCGA slide",
        tags=["tcga"],
        metadata={},
        geometry=PublicSampleGeometry(
            width_px=10, height_px=20, mpp_x=0.5, mpp_y=0.5
        ),
        markers=["CD3"],
        thumbnail_url="/thumbnail",
        pyramid_url="/zarr",
    )


def test_samples_get_prints_public_branch(fake_client: MagicMock) -> None:
    fake_client.samples.get.return_value = _public_sample()

    result = runner.invoke(_cli.app, ["samples", "get", "pub-1"])

    assert result.exit_code == 0, result.output
    assert '"ownership": "public"' in result.output
    assert '"id": "pub-1"' in result.output
    assert "public_id" not in result.output


def test_samples_get_downloads_public_branch(
    fake_client: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = _public_sample()
    dest = tmp_path / "store"
    download = MagicMock(return_value=dest)
    monkeypatch.setattr(PublicSample, "download_to", download)
    fake_client.samples.get.return_value = sample

    result = runner.invoke(
        _cli.app, ["samples", "get", "pub-1", "--download", str(dest)]
    )

    assert result.exit_code == 0, result.output
    download.assert_called_once_with(dest)
    assert str(dest) in result.output


def test_samples_patch_maps_name_tags_and_mpp(fake_client: MagicMock) -> None:
    fake_client.samples.patch.return_value = {"ownership": "mine", "id": "sample-1"}

    result = runner.invoke(
        _cli.app,
        [
            "samples",
            "patch",
            "sample-1",
            "--name",
            "Slide A",
            "--tag",
            "trial",
            "--tag",
            "site-a",
            "--mpp",
            "0.5",
        ],
    )

    assert result.exit_code == 0, result.output
    fake_client.samples.patch.assert_called_once_with(
        "sample-1", name="Slide A", tags=["trial", "site-a"], mpp=0.5
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--clear-name"], {"name": None}),
        (["--clear-tags"], {"tags": []}),
    ],
)
def test_samples_patch_clear_flags(
    fake_client: MagicMock, argv: list[str], expected: dict[str, object]
) -> None:
    fake_client.samples.patch.return_value = {"ownership": "mine", "id": "sample-1"}

    result = runner.invoke(_cli.app, ["samples", "patch", "sample-1", *argv])

    assert result.exit_code == 0, result.output
    fake_client.samples.patch.assert_called_once_with("sample-1", **expected)


@pytest.mark.parametrize(
    "argv",
    [
        ["--name", "x", "--clear-name"],
        ["--tag", "x", "--clear-tags"],
    ],
)
def test_samples_patch_rejects_mutually_exclusive_flags(
    fake_client: MagicMock, argv: list[str]
) -> None:
    result = runner.invoke(_cli.app, ["samples", "patch", "sample-1", *argv])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    fake_client.samples.patch.assert_not_called()


def test_samples_patch_conflicts_raise_value_error_locally() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _cli._sample_patch_updates(
            name=None, clear_name=False, tag=None, clear_tags=False, mpp=None
        )
    with pytest.raises(ValueError, match="--name and --clear-name"):
        _cli._sample_patch_updates(
            name="x", clear_name=True, tag=None, clear_tags=False, mpp=None
        )
    with pytest.raises(ValueError, match="--tag and --clear-tags"):
        _cli._sample_patch_updates(
            name=None, clear_name=False, tag=["x"], clear_tags=True, mpp=None
        )


def test_samples_patch_rejects_empty_update_before_auth(fake_client: MagicMock) -> None:
    result = runner.invoke(_cli.app, ["samples", "patch", "sample-1"])

    assert result.exit_code == 1
    assert "at least one" in result.output
    fake_client.samples.patch.assert_not_called()


# ---------- uploads list ----------


def test_uploads_list(fake_client: MagicMock) -> None:
    page = UploadList(
        uploads=[Upload(id="u1", filename="a.svs", status="ready")],
        next_cursor="c2",
    )
    fake_client.uploads.list.return_value = page

    result = runner.invoke(_cli.app, ["uploads", "list", "--limit", "50", "--cursor", "c1"])

    assert result.exit_code == 0, result.output
    fake_client.uploads.list.assert_called_once_with(limit=50, cursor="c1")
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


def test_public_subapp_is_absent() -> None:
    result = runner.invoke(_cli.app, ["public", "--help"])
    assert result.exit_code != 0


# ---------- help renders ----------


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["upload", "--help"],
        ["predict", "--help"],
        ["status", "--help"],
        ["wait", "--help"],
        ["cancel", "--help"],
        ["results", "--help"],
        ["ome-tiff", "--help"],
        ["samples", "--help"],
        ["samples", "list", "--help"],
        ["samples", "get", "--help"],
        ["samples", "patch", "--help"],
        ["uploads", "--help"],
        ["uploads", "list", "--help"],
    ],
)
def test_help_renders(argv: list[str]) -> None:
    result = runner.invoke(_cli.app, argv)
    assert result.exit_code == 0, result.output
    assert result.output


def test_new_cli_help_documents_dry_run_wait_cancel_and_ome_tiff() -> None:
    top = runner.invoke(_cli.app, ["--help"])
    assert top.exit_code == 0, top.output
    top_text = _plain(top.output)
    for command in ("wait", "cancel", "ome-tiff"):
        assert command in top_text

    predict_help = runner.invoke(_cli.app, ["predict", "--help"])
    predict_text = " ".join(_plain(predict_help.output).split())
    assert "--dry-run" in predict_text
    assert "without creating" in predict_text
    assert "a job or reserving credits" in predict_text

    wait_help = runner.invoke(_cli.app, ["wait", "--help"])
    wait_text = " ".join(_plain(wait_help.output).split())
    assert "--timeout" in wait_text
    assert "--poll-interval" in wait_text
    assert "SSE with polling fallback" in wait_text

    ome_help = _plain(runner.invoke(_cli.app, ["ome-tiff", "--help"]).output)
    assert "--out" in ome_help
    assert "--timeout" in ome_help
