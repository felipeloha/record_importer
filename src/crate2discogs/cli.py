from pathlib import Path
from typing import Annotated

import typer

from crate2discogs.checks import run_checks
from crate2discogs.config import ConfigError, Settings, load_settings
from crate2discogs.db import Repo, connect
from crate2discogs.discogs import DiscogsClient
from crate2discogs.evaluate import format_report, load_expected, run_eval
from crate2discogs.extract import make_extractor
from crate2discogs.extract.base import ExtractorUnavailable
from crate2discogs.importer import import_dir

app = typer.Typer(no_args_is_help=True, help="Turn photos of vinyl records into Discogs collection entries.")


def _todo(name: str) -> None:
    typer.echo(f"{name}: not implemented yet")
    raise typer.Exit(1)


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from None


def _repo(settings: Settings) -> Repo:
    return Repo(connect(settings.db_path))


def _work_dir(settings: Settings) -> Path:
    return settings.db_path.parent / "work"


def _client(settings: Settings, repo: Repo) -> DiscogsClient:
    return DiscogsClient(
        settings.discogs_token,
        settings.discogs_username,
        settings.user_agent,
        repo=repo,
        cache_ttl=settings.cache_ttl_days * 86400,
    )


@app.command()
def init() -> None:
    """Create the database and check config, Discogs token, folder and extractor."""
    settings = _settings()
    repo = _repo(settings)
    typer.echo(f"database ready: {settings.db_path}")
    checks = run_checks(settings, _client(settings, repo))
    for c in checks:
        typer.echo(f"{'ok  ' if c.ok else 'FAIL'} {c.name}: {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command(name="import")
def import_(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    recursive: Annotated[bool, typer.Option("--recursive")] = False,
) -> None:
    """Import photos from a directory."""
    settings = _settings()
    res = import_dir(_repo(settings), directory, _work_dir(settings), settings.max_edge, recursive)
    typer.echo(f"{res.new} new, {res.existing} already imported")


@app.command()
def identify() -> None:
    """Decode, extract, match and resolve imported photos."""
    _todo("identify")


@app.command()
def review() -> None:
    """Open the local review UI."""
    _todo("review")


@app.command(name="add-to-collection")
def add_to_collection() -> None:
    """Add resolved releases to the Discogs collection (dry run unless --apply)."""
    _todo("add-to-collection")


@app.command()
def status() -> None:
    """Show counts per stage and outcome."""
    _todo("status")


@app.command()
def export() -> None:
    """Export one row per photo to CSV."""
    _todo("export")


@app.command(name="eval")
def eval_(
    expected: Annotated[Path, typer.Option("--expected", exists=True, dir_okay=False)],
    db: Annotated[Path, typer.Option("--db", help="Separate DB so fixtures never reach the real one")] = Path(
        "eval.db"
    ),
) -> None:
    """Measure accuracy on a labelled fixture set."""
    settings = _settings()
    entries = load_expected(expected)
    try:
        report = run_eval(
            Repo(connect(db)), entries, make_extractor(settings), db.parent / "eval-work", settings.max_edge
        )
    except ExtractorUnavailable as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    typer.echo(format_report(report))


def main() -> None:
    app()
