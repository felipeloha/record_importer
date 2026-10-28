from pathlib import Path

import pytest
from typer.testing import CliRunner

from crate2discogs.cli import app


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "u")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "crate.db"))
    for name in ("crate.db", "crate.db-wal", "crate.db-shm", "crate2discogs.log", ".env", "keep.db"):
        (tmp_path / name).write_text("x")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / f"{'a' * 64}.jpg").write_text("x")
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "record.jpg").write_text("x")
    return tmp_path


def test_reset_deletes_only_tool_files(home: Path) -> None:
    result = CliRunner().invoke(app, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    for gone in ("crate.db", "crate.db-wal", "crate.db-shm", "crate2discogs.log", "work"):
        assert not (home / gone).exists(), gone
    for kept in (".env", "keep.db", "photos/record.jpg"):
        assert (home / kept).exists(), kept


def test_reset_keeps_foreign_files_in_work_dir(home: Path) -> None:
    (home / "work" / "my-notes.txt").write_text("mine")
    CliRunner().invoke(app, ["reset", "--yes"])
    assert (home / "work" / "my-notes.txt").exists()
    assert not (home / "work" / f"{'a' * 64}.jpg").exists()


def test_reset_asks_and_abort_keeps_everything(home: Path) -> None:
    result = CliRunner().invoke(app, ["reset"], input="n\n")
    assert result.exit_code != 0
    assert (home / "crate.db").exists()
    assert "Delete these?" in result.output


def test_reset_nothing_to_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "u")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "crate.db"))
    result = CliRunner().invoke(app, ["reset"])
    assert result.exit_code == 0
    assert "nothing to delete" in result.output
