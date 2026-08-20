"""``strand`` command-line interface.

A thin wrapper over :class:`strand.Client`. Every command maps one-to-one onto
an SDK method and reuses the same parameter names and semantics, so the
terminal surface can't drift from the library (or the MCP tools, which wrap the
same client).

Auth reads ``STRAND_API_KEY`` (and optionally ``STRAND_BASE_URL``) from the
environment, exactly like ``strand.Client()``.

Command map:

======================  ====================================================
CLI command             SDK call
======================  ====================================================
``strand upload``       ``client.uploads.upload_file(...)``
``strand markers``      ``client.markers.list()``
``strand predict``      ``client.predict.submit(...)``
``strand status``       ``client.jobs.get(...).status``
``strand wait``         ``client.jobs.get(...).wait(...)``
``strand cancel``       ``client.jobs.get(...).cancel()``
``strand results``      ``client.jobs.get(...).download_results(dir)``
``strand ome-tiff``     ``client.jobs.get(...).export_ome_tiff(...)``
``strand samples list`` ``client.samples.list(...)``
``strand samples get``  ``client.samples.get(...)``
``strand samples patch`` ``client.samples.patch(...)``
``strand uploads list`` ``client.uploads.list(...)``
=======================  ====================================================
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

import typer

from ._client import Client
from ._errors import StrandError
from ._public import PublicSample
from ._samples import SampleScope

app = typer.Typer(
    name="strand",
    help="Run H&E slides through Lattice from the terminal. Wraps the Strand SDK.",
    no_args_is_help=True,
    add_completion=False,
)

samples_app = typer.Typer(
    help="Inspect samples (uploaded slides).",
    no_args_is_help=True,
)
app.add_typer(samples_app, name="samples")

uploads_app = typer.Typer(
    help="Inspect resumable upload records.",
    no_args_is_help=True,
)
app.add_typer(uploads_app, name="uploads")


# ---------- shared helpers ----------


def _client() -> Client:
    """Construct a `Client` from the environment, or exit cleanly if unconfigured."""
    try:
        return Client()
    except StrandError as exc:
        _fail(str(exc))


def _fail(message: str) -> NoReturn:
    """Print a one-line error and exit non-zero. Never returns."""
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _encode(value: Any) -> Any:
    """Recursively JSON-ready a model / datetime / Path / container."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _encode(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _encode(v) for k, v in asdict(value).items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _emit(obj: Any) -> None:
    """Print `obj` as indented JSON to stdout."""
    typer.echo(json.dumps(_encode(obj), indent=2, default=str))


def _version_callback(value: bool) -> None:
    if value:
        from . import __version__

        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed strand-sdk version and exit.",
    ),
) -> None:
    """Strand CLI — the same verbs as the SDK and MCP tools."""


# ---------- commands ----------


@app.command()
def upload(
    file: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Local whole-slide image (SVS / TIFF / NDPI / ...).",
    ),
    content_type: str | None = typer.Option(
        None,
        "--content-type",
        help="MIME type override. Auto-detected from the file extension when omitted.",
    ),
    if_not_exists: bool = typer.Option(
        False,
        "--if-not-exists",
        help="Dedup by content hash; skip the byte upload if the slide already exists.",
    ),
    mpp: float | None = typer.Option(
        None,
        "--mpp",
        help="Microns per pixel for the slide (isotropic). Persisted at creation; "
        "takes precedence over the slide's own calibrated scale.",
    ),
) -> None:
    """Upload a local H&E slide and print its current ingest status.

    Dimensions may be absent until preprocessing finishes.
    """
    client = _client()
    try:
        result = client.uploads.upload_file(
            str(file),
            content_type=content_type,
            if_not_exists=if_not_exists,
            mpp=mpp,
        )
        _emit(result)
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command()
def predict(
    sample_id: str = typer.Argument(
        ...,
        metavar="SAMPLE_ID",
        help="Upload / sample id returned by `strand upload`.",
    ),
    markers: str = typer.Option(
        ...,
        "--markers",
        "-m",
        help="Comma-separated marker names, e.g. CD3e,CD8,PanCK.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Lattice version (e.g. v0.7). Server picks the current default when omitted.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate and price the request without creating a job or reserving credits.",
    ),
) -> None:
    """Estimate or submit a prediction job (H&E to multiplex)."""
    marker_list = [m.strip() for m in markers.split(",") if m.strip()]
    if not marker_list:
        raise typer.BadParameter("Provide at least one marker.", param_hint="--markers")
    client = _client()
    try:
        if dry_run:
            estimate = client.predict.submit(sample_id, marker_list, model=model, dry_run=True)
            _emit(estimate)
        else:
            job = client.predict.submit(sample_id, marker_list, model=model, dry_run=False)
            _emit({"job_id": job.id, "reserved_credits": job.reserved_credits})
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command()
def status(
    job_id: str = typer.Argument(..., help="Job id returned by `strand predict`."),
) -> None:
    """Print a point-in-time status snapshot for a job."""
    client = _client()
    try:
        job = client.jobs.get(job_id)
        _emit(job.status)
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command()
def wait(
    job_id: str = typer.Argument(..., help="Job id returned by `strand predict`."),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=0.0,
        help="Maximum seconds to wait. Omit to wait indefinitely.",
    ),
    poll_interval: float = typer.Option(
        2.0,
        "--poll-interval",
        min=0.001,
        help="Seconds between status requests when polling is needed.",
    ),
) -> None:
    """Wait for a terminal job status, using SSE with polling fallback."""
    client = _client()
    try:
        job = client.jobs.get(job_id)
        _emit(job.wait(timeout=timeout, poll_interval=poll_interval))
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command()
def cancel(
    job_id: str = typer.Argument(..., help="In-flight job id returned by `strand predict`."),
) -> None:
    """Cancel an eligible in-flight job and print its post-cancel status."""
    client = _client()
    try:
        job = client.jobs.get(job_id)
        _emit(job.cancel())
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command()
def results(
    job_id: str = typer.Argument(..., help="Job id returned by `strand predict`."),
    out: Path = typer.Option(
        Path("results"),
        "--out",
        "-o",
        help="Directory to write the OME-Zarr result tree into. Created if missing.",
    ),
) -> None:
    """Download a completed job's results (OME-Zarr) to a local directory."""
    client = _client()
    try:
        job = client.jobs.get(job_id)
        written = job.download_results(str(out))
        _emit({"path": str(written)})
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command("ome-tiff")
def ome_tiff(
    job_id: str = typer.Argument(..., help="Completed job id returned by `strand predict`."),
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        dir_okay=False,
        help="Destination OME-TIFF file path. Parent directories are created.",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        min=0.0,
        help="Maximum seconds to wait for export. Omit to wait indefinitely.",
    ),
    poll_interval: float = typer.Option(
        2.0,
        "--poll-interval",
        min=0.001,
        help="Seconds between export-status requests.",
    ),
) -> None:
    """Request, wait for, and download a completed job as OME-TIFF."""
    client = _client()
    try:
        job = client.jobs.get(job_id)
        written = job.export_ome_tiff(
            str(out), timeout=timeout, poll_interval=poll_interval
        )
        _emit({"path": str(written)})
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@app.command()
def markers() -> None:
    """List the markers your account can request (credit-free)."""
    client = _client()
    try:
        _emit(client.markers.list())
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@samples_app.command("list")
def samples_list(
    scope: SampleScope = typer.Option("mine", "--scope", help="Sample scope."),
    limit: int = typer.Option(48, "--limit", help="Page size (API range 1-100)."),
    cursor: str | None = typer.Option(
        None, "--cursor", help="Opaque pagination cursor from a prior response."
    ),
    tag: str | None = typer.Option(None, "--tag", help="Exact sample-tag filter."),
) -> None:
    """List owned samples, the public cohort, or both."""
    client = _client()
    try:
        _emit(client.samples.list(scope=scope, limit=limit, cursor=cursor, tag=tag))
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@samples_app.command("get")
def samples_get(
    sample_id: str = typer.Argument(..., metavar="SAMPLE_ID", help="Owned or public sample id."),
    download: Path | None = typer.Option(
        None,
        "--download",
        "-d",
        help="For a public sample, mirror its OME-Zarr store into this directory.",
    ),
) -> None:
    """Show owned detail with job history or public detail by share id."""
    client = _client()
    try:
        sample = client.samples.get(sample_id)
        if download is not None:
            if not isinstance(sample, PublicSample):
                raise ValueError("--download is available only for public samples")
            detail = sample.to_dict()
            detail["downloaded_to"] = str(sample.download_to(download))
            _emit(detail)
        else:
            _emit(sample)
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


def _sample_patch_updates(
    *,
    name: str | None,
    clear_name: bool,
    tag: list[str] | None,
    clear_tags: bool,
    mpp: float | None,
) -> dict[str, Any]:
    if name is not None and clear_name:
        raise ValueError("--name and --clear-name are mutually exclusive")
    if tag is not None and clear_tags:
        raise ValueError("--tag and --clear-tags are mutually exclusive")

    updates: dict[str, Any] = {}
    if clear_name:
        updates["name"] = None
    elif name is not None:
        updates["name"] = name
    if clear_tags:
        updates["tags"] = []
    elif tag is not None:
        updates["tags"] = tag
    if mpp is not None:
        updates["mpp"] = mpp
    if not updates:
        raise ValueError("Provide at least one sample field to patch")
    return updates


@samples_app.command("patch")
def samples_patch(
    sample_id: str = typer.Argument(..., metavar="SAMPLE_ID", help="Owned sample id."),
    name: str | None = typer.Option(None, "--name", help="Set the display name."),
    clear_name: bool = typer.Option(False, "--clear-name", help="Revert to the filename."),
    tag: list[str] | None = typer.Option(
        None, "--tag", help="Complete desired tag set; repeat for multiple tags."
    ),
    clear_tags: bool = typer.Option(False, "--clear-tags", help="Clear all editable tags."),
    mpp: float | None = typer.Option(None, "--mpp", help="Set isotropic microns per pixel."),
) -> None:
    """Patch an owned sample's name, complete tag set, and/or physical scale."""
    try:
        updates = _sample_patch_updates(
            name=name,
            clear_name=clear_name,
            tag=tag,
            clear_tags=clear_tags,
            mpp=mpp,
        )
    except ValueError as exc:
        _fail(str(exc))

    client = _client()
    try:
        _emit(client.samples.patch(sample_id, **updates))
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


@uploads_app.command("list")
def uploads_list(
    limit: int = typer.Option(100, "--limit", help="Page size (1-200)."),
    cursor: str | None = typer.Option(
        None, "--cursor", help="Opaque pagination cursor from a prior response."
    ),
) -> None:
    """List resumable upload records for your organization."""
    client = _client()
    try:
        _emit(client.uploads.list(limit=limit, cursor=cursor))
    except (StrandError, ValueError) as exc:
        _fail(str(exc))
    finally:
        client.close()


def main() -> None:
    """Console-script entry point (`strand`)."""
    app()


if __name__ == "__main__":
    main()
