from pathlib import Path

from crate2discogs.db import BarcodeRow, Repo, connect
from crate2discogs.pipeline import run_decode


def _repo_with_photos(tmp_path: Path, n: int) -> Repo:
    repo = Repo(connect(tmp_path / "t.db"))
    for i in range(n):
        repo.upsert_photo(f"{i:064d}", tmp_path / f"{i}.jpg", tmp_path / f"w{i}.jpg")
    return repo


def test_decode_stores_barcodes_and_advances(tmp_path: Path) -> None:
    repo = _repo_with_photos(tmp_path, 2)
    n = run_decode(repo, decoder=lambda p: [BarcodeRow("EAN13", "4036909000014", True)])
    assert n == 2
    for p in repo.all_photos():
        assert p.stage == "decoded"
        assert repo.get_barcodes(p.id) == [BarcodeRow("EAN13", "4036909000014", True)]


def test_decode_error_is_stored_and_others_continue(tmp_path: Path) -> None:
    repo = _repo_with_photos(tmp_path, 3)

    def decoder(path: Path) -> list[BarcodeRow]:
        if path.name == "1.jpg":
            raise OSError("cannot read")
        return []

    assert run_decode(repo, decoder=decoder) == 2
    stages = {p.path.name: (p.stage, p.last_error) for p in repo.all_photos()}
    assert stages["0.jpg"] == ("decoded", None)
    assert stages["1.jpg"] == ("imported", "decode: cannot read")
    assert stages["2.jpg"] == ("decoded", None)


def test_decode_limit(tmp_path: Path) -> None:
    repo = _repo_with_photos(tmp_path, 3)
    assert run_decode(repo, limit=2, decoder=lambda p: []) == 2
    assert len(repo.photos_at_stage("imported")) == 1
