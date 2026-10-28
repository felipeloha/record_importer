import sqlite3
from pathlib import Path

import pytest

from crate2discogs.db import (
    LATEST_VERSION,
    BarcodeRow,
    CandidateRow,
    CollectionAddRow,
    ExtractionRow,
    IllegalTransition,
    Repo,
    ResolutionRow,
    connect,
    migrate,
)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(connect(tmp_path / "t.db"))


def _photo(repo: Repo, sha: str = "a" * 64) -> int:
    pid, _ = repo.upsert_photo(sha, Path(f"/p/{sha[:4]}.jpg"), Path(f"/w/{sha[:4]}.jpg"))
    return pid


def test_fresh_db_migrates_to_latest(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION


def test_migrate_twice_is_noop(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION


def test_same_sha_twice_one_row(repo: Repo) -> None:
    a, created_a = repo.upsert_photo("x" * 64, Path("/p/a.jpg"), Path("/w/a.jpg"))
    b, created_b = repo.upsert_photo("x" * 64, Path("/p/b.jpg"), Path("/w/b.jpg"))
    assert a == b
    assert (created_a, created_b) == (True, False)
    assert repo.count_photos() == 1


def test_legal_stage_path(repo: Repo) -> None:
    pid = _photo(repo)
    for stage in ("decoded", "extracted", "matched", "resolved", "added"):
        repo.set_stage(pid, stage)
    photo = repo.get_photo(pid)
    assert photo is not None
    assert photo.stage == "added"


def test_extract_failed_can_be_retried(repo: Repo) -> None:
    pid = _photo(repo)
    repo.set_stage(pid, "decoded")
    repo.set_stage(pid, "extract_failed")
    repo.set_stage(pid, "extracted")


@pytest.mark.parametrize(
    ("path", "illegal"),
    [((), "added"), ((), "matched"), (("decoded",), "resolved"), (("decoded", "extract_failed"), "matched")],
)
def test_illegal_stage_jump_rejected(repo: Repo, path: tuple[str, ...], illegal: str) -> None:
    pid = _photo(repo)
    for stage in path:
        repo.set_stage(pid, stage)
    with pytest.raises(IllegalTransition):
        repo.set_stage(pid, illegal)


def test_photos_at_stage_with_limit(repo: Repo) -> None:
    ids = [_photo(repo, c * 64) for c in "abc"]
    repo.set_stage(ids[1], "decoded")
    assert [p.id for p in repo.photos_at_stage("imported")] == [ids[0], ids[2]]
    assert [p.id for p in repo.photos_at_stage("imported", limit=1)] == [ids[0]]


def test_last_error_roundtrip(repo: Repo) -> None:
    pid = _photo(repo)
    repo.set_error(pid, "boom")
    photo = repo.get_photo(pid)
    assert photo is not None
    assert photo.last_error == "boom"
    repo.set_error(pid, None)
    photo = repo.get_photo(pid)
    assert photo is not None
    assert photo.last_error is None


def test_barcodes_replaced(repo: Repo) -> None:
    pid = _photo(repo)
    repo.replace_barcodes(
        pid, [BarcodeRow("EAN13", "4250101497384", True), BarcodeRow("Code128", "1", False)]
    )
    repo.replace_barcodes(pid, [BarcodeRow("EAN13", "4036909000014", True)])
    assert repo.get_barcodes(pid) == [BarcodeRow("EAN13", "4036909000014", True)]


def test_extraction_one_row_latest_values(repo: Repo) -> None:
    pid = _photo(repo)
    repo.save_extraction(pid, ExtractionRow("ollama", "m1", "{}", None, "bad json"))
    second = ExtractionRow("ollama", "m2", '{"a":1}', '{"a":1}', None)
    repo.save_extraction(pid, second)
    assert repo.get_extraction(pid) == second
    assert repo.conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1


def test_candidates_replaced(repo: Repo) -> None:
    pid = _photo(repo)
    repo.replace_candidates(pid, [CandidateRow(1, 0, 0.5, '{"catno":"X"}', "{}")])
    new = [CandidateRow(2, 0, 0.9, '{"barcode":"1"}', '{"id":2}'), CandidateRow(3, 1, 0.1, "{}", "{}")]
    repo.replace_candidates(pid, new)
    assert repo.get_candidates(pid) == new


def test_resolution_one_row_latest_values(repo: Repo) -> None:
    pid = _photo(repo)
    assert repo.save_resolution(pid, ResolutionRow("review", None, "auto"))
    assert repo.save_resolution(pid, ResolutionRow("auto", 7, "auto"))
    res = repo.get_resolution(pid)
    assert res is not None
    assert (res.outcome, res.release_id, res.resolved_by) == ("auto", 7, "auto")
    assert repo.conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0] == 1


def test_auto_resolution_never_overwrites_user(repo: Repo) -> None:
    pid = _photo(repo)
    repo.save_resolution(pid, ResolutionRow("auto", 1, "user"))
    assert repo.save_resolution(pid, ResolutionRow("review", 2, "auto")) is False
    res = repo.get_resolution(pid)
    assert res is not None
    assert (res.release_id, res.resolved_by) == (1, "user")
    assert repo.save_resolution(pid, ResolutionRow("skip", None, "user")) is True


def test_copies_default_and_set(repo: Repo) -> None:
    assert repo.get_copies(5) == 1
    repo.set_copies(5, 3)
    assert repo.get_copies(5) == 3
    with pytest.raises(ValueError):
        repo.set_copies(5, 0)


def test_collection_adds_roundtrip(repo: Repo) -> None:
    repo.record_collection_add(10, 999, 1)
    rows = repo.collection_adds(10)
    assert rows == [CollectionAddRow(10, 999, 1, rows[0].added_at)]


def test_cache_roundtrip(repo: Repo) -> None:
    assert repo.cache_get("k") is None
    repo.cache_put("k", 200, "body")
    entry = repo.cache_get("k")
    assert entry is not None
    assert (entry.status, entry.body) == (200, "body")
    repo.cache_put("k", 200, "body2")
    entry = repo.cache_get("k")
    assert entry is not None
    assert entry.body == "body2"


def test_photo_fk_enforced(repo: Repo) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo.replace_barcodes(12345, [BarcodeRow("EAN13", "1", True)])
