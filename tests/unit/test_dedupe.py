from pathlib import Path

import pytest

from crate2discogs.db import Outcome, Repo, ResolutionRow, ResolvedBy, connect
from crate2discogs.dedupe import groups


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(connect(tmp_path / "t.db"))


def _resolved(repo: Repo, i: int, outcome: Outcome, release_id: int | None, by: ResolvedBy = "auto") -> int:
    pid, _ = repo.upsert_photo(f"{i:064d}", Path(f"/{i}.jpg"), Path(f"/w{i}.jpg"))
    repo.save_resolution(pid, ResolutionRow(outcome, release_id, by))
    return pid


def test_two_photos_same_release_one_group(repo: Repo) -> None:
    a = _resolved(repo, 1, "auto", 301)
    b = _resolved(repo, 2, "auto", 301)
    (g,) = groups(repo)
    assert (g.release_id, g.photo_ids, g.copies) == (301, (a, b), 1)
    assert g.needs_copy_question


def test_copies_set_by_user(repo: Repo) -> None:
    _resolved(repo, 1, "auto", 301)
    repo.set_copies(301, 2)
    (g,) = groups(repo)
    assert g.copies == 2


def test_different_releases_separate_groups(repo: Repo) -> None:
    _resolved(repo, 1, "auto", 301)
    _resolved(repo, 2, "auto", 201)
    gs = groups(repo)
    assert [g.release_id for g in gs] == [201, 301]
    assert not any(g.needs_copy_question for g in gs)


def test_excluded_outcomes(repo: Repo) -> None:
    _resolved(repo, 1, "review", None)
    _resolved(repo, 2, "not_found", None)
    _resolved(repo, 3, "skip", None, "user")
    assert groups(repo) == []


def test_user_pick_counts(repo: Repo) -> None:
    pid = _resolved(repo, 1, "review", None)
    repo.save_resolution(pid, ResolutionRow("review", 999, "user"))
    (g,) = groups(repo)
    assert g.release_id == 999


def test_skip_after_auto_excludes(repo: Repo) -> None:
    pid = _resolved(repo, 1, "auto", 301)
    repo.save_resolution(pid, ResolutionRow("skip", 301, "user"))
    assert groups(repo) == []
