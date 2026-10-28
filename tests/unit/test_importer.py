from pathlib import Path

import pillow_heif
import pytest
from PIL import Image
from typer.testing import CliRunner

from crate2discogs.cli import app
from crate2discogs.db import Repo, connect
from crate2discogs.importer import import_dir

pillow_heif.register_heif_opener()


def _img(path: Path, size: tuple[int, int] = (40, 20), orientation: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (200, 30, 30))
    # Vary a pixel per filename so distinct files get distinct hashes.
    img.putpixel((0, 0), (len(path.name) % 256, sum(map(ord, path.name)) % 256, 0))
    if path.suffix.lower() in {".heic", ".heif"}:
        img.save(path, format="HEIF")
        return path
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    img.save(path, exif=exif)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(connect(tmp_path / "t.db"))


def test_filters_extensions(tmp_path: Path, repo: Repo) -> None:
    src = tmp_path / "in"
    _img(src / "a.jpg")
    _img(src / "b.HEIC")
    _img(src / "c.png")
    _img(src / "d.webp")
    (src / "notes.txt").write_text("hi")
    res = import_dir(repo, src, tmp_path / "work", max_edge=1600)
    assert res.new == 4
    assert repo.count_photos() == 4
    for p in repo.all_photos():
        assert p.work_path.suffix == ".jpg"
        with Image.open(p.work_path) as w:
            assert w.format == "JPEG"


def test_same_content_two_names_one_photo(tmp_path: Path, repo: Repo) -> None:
    src = tmp_path / "in"
    a = _img(src / "a.jpg")
    (src / "copy.jpg").write_bytes(a.read_bytes())
    res = import_dir(repo, src, tmp_path / "work", max_edge=1600)
    assert (res.new, res.existing) == (1, 1)


def test_rerun_creates_nothing(tmp_path: Path, repo: Repo) -> None:
    src = tmp_path / "in"
    _img(src / "a.jpg")
    _img(src / "b.png")
    import_dir(repo, src, tmp_path / "work", max_edge=1600)
    res = import_dir(repo, src, tmp_path / "work", max_edge=1600)
    assert (res.new, res.existing) == (0, 2)
    assert repo.count_photos() == 2


def test_exif_orientation_applied(tmp_path: Path, repo: Repo) -> None:
    _img(tmp_path / "in" / "r.jpg", size=(40, 20), orientation=6)
    import_dir(repo, tmp_path / "in", tmp_path / "work", max_edge=1600)
    (photo,) = repo.all_photos()
    with Image.open(photo.work_path) as w:
        assert w.size == (20, 40)


def test_downscale_keeps_original(tmp_path: Path, repo: Repo) -> None:
    src = _img(tmp_path / "in" / "big.jpg", size=(4000, 3000))
    before = src.read_bytes()
    import_dir(repo, tmp_path / "in", tmp_path / "work", max_edge=1600)
    (photo,) = repo.all_photos()
    with Image.open(photo.work_path) as w:
        assert max(w.size) == 1600
        assert w.size == (1600, 1200)
    assert src.read_bytes() == before
    assert photo.path == src.resolve()


def test_recursive(tmp_path: Path, repo: Repo) -> None:
    _img(tmp_path / "in" / "top.jpg")
    _img(tmp_path / "in" / "sub" / "nested.jpg")
    assert import_dir(repo, tmp_path / "in", tmp_path / "work", max_edge=1600).new == 1
    assert import_dir(repo, tmp_path / "in", tmp_path / "work", max_edge=1600, recursive=True).new == 1
    assert repo.count_photos() == 2


def test_hidden_files_ignored(tmp_path: Path, repo: Repo) -> None:
    _img(tmp_path / "in" / "._a.jpg")
    assert import_dir(repo, tmp_path / "in", tmp_path / "work", max_edge=1600).new == 0


def test_cli_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "u")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "crate.db"))
    _img(tmp_path / "in" / "a.jpg")
    result = CliRunner().invoke(app, ["import", str(tmp_path / "in")])
    assert result.exit_code == 0, result.output
    assert "1 new" in result.output
    assert Repo(connect(tmp_path / "crate.db")).count_photos() == 1
