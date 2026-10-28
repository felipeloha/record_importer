from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="Turn photos of vinyl records into Discogs collection entries.")


def _todo(name: str) -> None:
    typer.echo(f"{name}: not implemented yet")
    raise typer.Exit(1)


@app.command()
def init() -> None:
    """Create the database and check the configuration."""
    _todo("init")


@app.command(name="import")
def import_(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    recursive: Annotated[bool, typer.Option("--recursive")] = False,
) -> None:
    """Import photos from a directory."""
    _todo("import")


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
