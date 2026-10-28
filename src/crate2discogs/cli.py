import logging
import re
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer

from crate2discogs.add_to_collection import apply_plan, build_plan, format_plan, pending_groups
from crate2discogs.checks import run_checks
from crate2discogs.config import ConfigError, Settings, load_settings
from crate2discogs.db import Repo, connect
from crate2discogs.discogs import DiscogsClient, DiscogsError
from crate2discogs.evaluate import format_report, load_expected, run_eval
from crate2discogs.extract import make_extractor
from crate2discogs.extract.base import ExtractorUnavailable
from crate2discogs.importer import import_dir
from crate2discogs.pipeline import StepName
from crate2discogs.pipeline import identify as identify_photos
from crate2discogs.report import export_csv, format_status, status_counts
from crate2discogs.review.app import HOST, PORT, create_app

app = typer.Typer(no_args_is_help=True, help="Turn photos of vinyl records into Discogs collection entries.")


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


def _log_path(settings: Settings) -> Path:
    return settings.db_path.parent / "crate2discogs.log"


def _log_to_file(settings: Settings) -> Path:
    path = _log_path(settings)
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("crate2discogs")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    return path


@app.command()
def identify(
    stage: Annotated[StepName | None, typer.Option("--stage", help="Run only this step")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1, help="Max photos per step")] = None,
    always_extract: Annotated[bool, typer.Option("--always-extract")] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Redo match/resolve for photos already past them (not added ones). Keeps user choices",
        ),
    ] = False,
    retry_skipped: Annotated[
        bool,
        typer.Option(
            "--retry-skipped",
            help="Remove your 'skip' decisions (picks are kept) and redo match/resolve, like --force",
        ),
    ] = False,
    allow_api_billing: Annotated[
        bool,
        typer.Option(
            "--allow-api-billing", help="Let the claude-code extractor run with ANTHROPIC_API_KEY set"
        ),
    ] = False,
) -> None:
    """Decode, extract, match and resolve imported photos. Resumable."""
    settings = _settings()
    repo = _repo(settings)
    log_path = _log_to_file(settings)
    if retry_skipped:
        typer.echo(f"cleared {repo.clear_user_skips()} skip decisions; searching again")
        force = True

    with ExitStack() as stack:

        def progress(name: StepName, total: int) -> Callable[[], None]:
            bar = stack.enter_context(typer.progressbar(length=total, label=f"{name:<8}"))
            return lambda: bar.update(1)

        try:
            done = identify_photos(
                repo,
                extractor=lambda: make_extractor(settings, allow_api_billing),
                search=lambda: _client(settings, repo).search,
                margin=settings.auto_margin,
                budget=settings.query_budget,
                variant_cap=settings.catno_variant_cap,
                step=stage,
                limit=limit,
                always_extract=always_extract,
                force=force,
                progress=progress,
            )
        except (ExtractorUnavailable, DiscogsError) as e:
            stack.close()
            typer.echo(f"\nstopped: {e}\nre-run `identify` to continue where it stopped.", err=True)
            raise typer.Exit(1) from None
    typer.echo(" ".join(f"{k}={v}" for k, v in done.items()) + f"   (log: {log_path})")
    typer.echo(format_status(status_counts(repo)))


@app.command()
def review(
    port: Annotated[int, typer.Option("--port")] = PORT,
    no_browser: Annotated[bool, typer.Option("--no-browser")] = False,
) -> None:
    """Open the local review UI (localhost only). Choices are saved immediately."""
    import threading
    import webbrowser

    import uvicorn

    settings = _settings()
    repo = _repo(settings)
    web = create_app(repo, _client(settings, repo), settings.auto_margin)
    url = f"http://{HOST}:{port}/"
    typer.echo(f"review UI: {url}  (Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(web, host=HOST, port=port, log_level="warning")


@app.command(name="add-to-collection")
def add_to_collection(
    apply: Annotated[bool, typer.Option("--apply", help="Write to Discogs. Without it: dry run")] = False,
) -> None:
    """Add resolved releases to the Discogs collection (dry run unless --apply)."""
    settings = _settings()
    repo = _repo(settings)
    _log_to_file(settings)
    client = _client(settings, repo)
    total = len(pending_groups(repo))
    with typer.progressbar(length=total, label="checking collection") as bar:
        rows = build_plan(repo, client, tick=lambda: bar.update(1))
    typer.echo(format_plan(rows))
    if not apply:
        typer.echo("\ndry run: nothing was written. Re-run with --apply to add to the collection.")
        return
    with typer.progressbar(length=len(rows), label=f"adding to folder {settings.discogs_folder_id}") as bar:
        summary = apply_plan(repo, client, settings.discogs_folder_id, rows, tick=lambda: bar.update(1))
    typer.echo(f"\nadded {summary.added} instances, skipped {summary.skipped}, failed {summary.failed}")
    if summary.failed:
        typer.echo("re-run `add-to-collection --apply` to retry the failed ones.")
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show counts per stage and outcome."""
    typer.echo(format_status(status_counts(_repo(_settings()))))


@app.command()
def export(csv_path: Annotated[Path, typer.Option("--csv", dir_okay=False)]) -> None:
    """Export one row per photo to CSV."""
    n = export_csv(_repo(_settings()), csv_path)
    typer.echo(f"wrote {n} rows to {csv_path}")


_WORK_COPY = re.compile(r"[0-9a-f]{64}\.jpg")


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation")] = False,
) -> None:
    """Delete the database, image work copies and log to start from scratch. Photos and .env are kept."""
    settings = _settings()
    db = settings.db_path
    files = [f for f in (db, Path(f"{db}-wal"), Path(f"{db}-shm"), _log_path(settings)) if f.exists()]
    work = _work_dir(settings)
    copies = [f for f in work.iterdir() if _WORK_COPY.fullmatch(f.name)] if work.is_dir() else []
    if not files and not copies:
        typer.echo("nothing to delete")
        return
    for f in files:
        typer.echo(f"  {f}")
    if copies:
        typer.echo(f"  {work}/  ({len(copies)} image work copies)")
    typer.echo("All decisions, review choices and cached Discogs results in the database are lost.")
    if not yes and not typer.confirm("Delete these?"):
        raise typer.Abort()
    for f in [*files, *copies]:
        f.unlink()
    if work.is_dir() and not any(work.iterdir()):
        work.rmdir()
    typer.echo("done. Start again with `crate2discogs import <dir>`.")


@app.command(name="eval")
def eval_(
    expected: Annotated[Path, typer.Option("--expected", exists=True, dir_okay=False)],
    db: Annotated[Path, typer.Option("--db", help="Separate DB so fixtures never reach the real one")] = Path(
        "eval.db"
    ),
    extract_only: Annotated[bool, typer.Option("--extract-only", help="Skip Discogs match/resolve")] = False,
    allow_api_billing: Annotated[
        bool,
        typer.Option(
            "--allow-api-billing", help="Let the claude-code extractor run with ANTHROPIC_API_KEY set"
        ),
    ] = False,
) -> None:
    """Measure accuracy on a labelled fixture set."""
    settings = _settings()
    entries = load_expected(expected)
    repo = Repo(connect(db))
    search = None if extract_only else _client(settings, repo).search
    try:
        report = run_eval(
            repo,
            entries,
            make_extractor(settings, allow_api_billing),
            db.parent / "eval-work",
            settings.max_edge,
            search=search,
            margin=settings.auto_margin,
            budget=settings.query_budget,
        )
    except (ExtractorUnavailable, DiscogsError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    typer.echo(format_report(report))


def main() -> None:
    app()
