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
``strand predict``      ``client.predict.submit(...)``
``strand status``       ``client.jobs.get(...).status``
``strand results``      ``client.jobs.get(...).download_results(dir)``
``strand samples list`` ``client.uploads.list(...)``
======================  ====================================================
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

public_app = typer.Typer(
    help="Browse and read the free, credit-less public cohort.",
    no_args_is_help=True,
)
app.add_typer(public_app, name="public")


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
    """Recursively JSON-ready a dataclass / datetime / Path / container."""
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
    """Upload a local H&E slide. Prints the upload (sample) id and slide dimensions."""
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
) -> None:
    """Submit a prediction job (H&E to multiplex). Reserves credits, prints the job id."""
    marker_list = [m.strip() for m in markers.split(",") if m.strip()]
    if not marker_list:
        raise typer.BadParameter("Provide at least one marker.", param_hint="--markers")
    client = _client()
    try:
        job = client.predict.submit(sample_id, marker_list, model=model)
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


@samples_app.command("list")
def samples_list(
    limit: int = typer.Option(100, "--limit", help="Page size (1-200)."),
    cursor: str | None = typer.Option(
        None, "--cursor", help="Opaque pagination cursor from a prior response."
    ),
) -> None:
    """List uploaded samples for your org, newest first."""
    client = _client()
    try:
        page = client.uploads.list(limit=limit, cursor=cursor)
        _emit(page)
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@public_app.command("list")
def public_list(
    page: int | None = typer.Option(None, "--page", help="1-based page number (default 1)."),
    page_size: int | None = typer.Option(
        None, "--page-size", help="Items per page (default 48, max 100)."
    ),
    tag: str | None = typer.Option(None, "--tag", help="Filter by a public display tag."),
) -> None:
    """List the free public cohort (paginated, newest first)."""
    client = _client()
    try:
        _emit(client.public.list(page=page, page_size=page_size, tag=tag))
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


@public_app.command("get")
def public_get(
    public_id: str = typer.Argument(..., help="Public id from `strand public list`."),
    download: Path | None = typer.Option(
        None,
        "--download",
        "-d",
        help="Directory to mirror the sample's OME-Zarr (H&E + markers) into.",
    ),
) -> None:
    """Show a public sample's detail; with --download, materialize its marker data."""
    client = _client()
    try:
        sample = client.public.get(public_id)
        detail: dict[str, Any] = {
            "public_id": sample.public_id,
            "title": sample.title,
            "tags": sample.tags,
            "metadata": sample.metadata,
            "geometry": sample.geometry,
            "markers": sample.markers,
        }
        if download is not None:
            detail["downloaded_to"] = str(sample.download_to(download))
        _emit(detail)
    except StrandError as exc:
        _fail(str(exc))
    finally:
        client.close()


def main() -> None:
    """Console-script entry point (`strand`)."""
    app()


if __name__ == "__main__":
    main()
