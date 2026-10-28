from pathlib import Path
from typing import Annotated

import typer

from crate2discogs.config import ConfigError, Settings, load_settings
from crate2discogs.db import Repo, connect
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


@app.command()
def init() -> None:
    """Create the database and check the configuration."""
    settings = _settings()
    _repo(settings)
    typer.echo(f"database ready: {settings.db_path}")


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
def eval_() -> None:
    """Measure accuracy on a labelled fixture set."""
    _todo("eval")


def main() -> None:
    app()
