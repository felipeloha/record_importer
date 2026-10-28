from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from crate2discogs.add_to_collection import apply_plan, build_plan, format_plan
from crate2discogs.cli import app
from crate2discogs.db import Repo, ResolutionRow, connect
from crate2discogs.discogs import DiscogsClient
from crate2discogs.ratelimit import TokenBucket

API = "https://api.discogs.com"
COLL = f"{API}/users/me/collection"


def _client(repo: Repo) -> DiscogsClient:
    noop = lambda s: None  # noqa: E731
    return DiscogsClient("t", "me", "ua", repo=repo, bucket=TokenBucket(1000, 1.0, sleep=noop), sleep=noop)


def _resolved(repo: Repo, i: int, release_id: int) -> int:
    pid, _ = repo.upsert_photo(f"{i:064d}", Path(f"/{i}.jpg"), Path(f"/w{i}.jpg"))
    for stage in ("decoded", "extracted", "matched", "resolved"):
        repo.set_stage(pid, stage)  # type: ignore[arg-type]
    repo.save_resolution(pid, ResolutionRow("auto", release_id, "auto"))
    return pid


class Discogs:
    """In-memory collection behind respx routes."""

    def __init__(self, existing: dict[int, int] | None = None, fail_post_for: set[int] | None = None) -> None:
        self.instances = dict(existing or {})
        self.fail_post_for = fail_post_for or set()
        self.posts: list[tuple[int, int]] = []
        self.next_instance = 1000

    def install(self, router: respx.Router) -> None:
        router.get(url__regex=rf"{COLL}/releases/(?P<rid>\d+)$").mock(side_effect=self._get)
        router.post(url__regex=rf"{COLL}/folders/(?P<fid>\d+)/releases/(?P<rid>\d+)$").mock(
            side_effect=self._post
        )

    def _get(self, request: httpx.Request, rid: str) -> httpx.Response:
        n = self.instances.get(int(rid), 0)
        return httpx.Response(200, json={"releases": [{"instance_id": i} for i in range(n)]})

    def _post(self, request: httpx.Request, fid: str, rid: str) -> httpx.Response:
        if int(rid) in self.fail_post_for:
            return httpx.Response(400, json={"message": "bad"})
        self.posts.append((int(fid), int(rid)))
        self.instances[int(rid)] = self.instances.get(int(rid), 0) + 1
        self.next_instance += 1
        return httpx.Response(201, json={"instance_id": self.next_instance})


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(connect(tmp_path / "t.db"))


def test_dry_run_zero_posts(repo: Repo) -> None:
    _resolved(repo, 1, 10)
    _resolved(repo, 2, 10)
    _resolved(repo, 3, 20)
    repo.set_copies(20, 2)
    d = Discogs({20: 1})
    with respx.mock(assert_all_called=False) as router:
        d.install(router)
        rows = build_plan(repo, _client(repo))
        assert not any(c.request.method == "POST" for c in router.calls)  # AC-6
    assert [(r.release_id, r.wanted, r.already, r.to_add) for r in rows] == [(10, 1, 0, 1), (20, 2, 1, 1)]
    assert "2 releases, 2 instances to add" in format_plan(rows)
    assert all(p.stage == "resolved" for p in repo.all_photos())


def test_apply_skips_existing_and_adds_missing_copies(repo: Repo) -> None:
    a = _resolved(repo, 1, 10)
    b = _resolved(repo, 2, 20)
    repo.set_copies(20, 2)
    d = Discogs({10: 1, 20: 1})
    with respx.mock as router:
        d.install(router)
        client = _client(repo)
        summary = apply_plan(repo, client, 5, build_plan(repo, client))
    assert d.posts == [(5, 20)]
    assert (summary.added, summary.skipped, summary.failed) == (1, 1, 0)
    assert [r.instance_id for r in repo.collection_adds(20)] == [1001]
    assert {repo.get_photo(a).stage, repo.get_photo(b).stage} == {"added"}  # type: ignore[union-attr]


def test_failure_then_rerun_no_double_add(repo: Repo) -> None:
    for i, rid in enumerate([1, 2, 3, 4, 5]):
        _resolved(repo, i, rid)
    d = Discogs(fail_post_for={3})
    with respx.mock as router:
        d.install(router)
        client = _client(repo)
        s1 = apply_plan(repo, client, 1, build_plan(repo, client))
        assert (s1.added, s1.failed) == (4, 1)
        d.fail_post_for = set()
        s2 = apply_plan(repo, client, 1, build_plan(repo, client))
    assert s2.added == 1
    assert sorted(rid for _, rid in d.posts) == [1, 2, 3, 4, 5]  # AC-7: each exactly once


def test_crash_after_post_before_db_write(repo: Repo) -> None:
    _resolved(repo, 1, 10)
    d = Discogs()
    with respx.mock as router:
        d.install(router)
        client = _client(repo)
        client.add_release(1, 10)  # Discogs got the POST; the process died before recording it
        apply_plan(repo, client, 1, build_plan(repo, client))  # re-run: fresh plan sees the instance
    assert d.posts == [(1, 10)]


def test_failed_lookup_is_not_marked_added(repo: Repo) -> None:
    pid = _resolved(repo, 1, 10)
    with respx.mock as router:
        router.get(f"{COLL}/releases/10").mock(return_value=httpx.Response(502))
        client = _client(repo)
        rows = build_plan(repo, client)
        summary = apply_plan(repo, client, 1, rows)
    assert rows[0].already is None and rows[0].error
    assert (summary.added, summary.skipped, summary.failed) == (0, 0, 1)
    assert repo.get_photo(pid).stage == "resolved"  # type: ignore[union-attr]


def test_fully_added_groups_are_done(repo: Repo) -> None:
    _resolved(repo, 1, 10)
    d = Discogs()
    with respx.mock as router:
        d.install(router)
        client = _client(repo)
        apply_plan(repo, client, 1, build_plan(repo, client))
        assert build_plan(repo, client) == []


def test_skip_and_review_never_added(repo: Repo) -> None:
    a = _resolved(repo, 1, 10)
    repo.save_resolution(a, ResolutionRow("skip", None, "user"))
    pid, _ = repo.upsert_photo("f" * 64, Path("/x.jpg"), Path("/wx.jpg"))
    repo.save_resolution(pid, ResolutionRow("review", None, "auto"))
    with respx.mock:
        assert build_plan(repo, _client(repo)) == []


def test_cli_dry_run_and_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "me")
    monkeypatch.setenv("DISCOGS_FOLDER_ID", "77")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "crate.db"))
    repo = Repo(connect(tmp_path / "crate.db"))
    _resolved(repo, 1, 10)
    d = Discogs()
    with respx.mock(assert_all_called=False) as router:
        d.install(router)
        dry = CliRunner().invoke(app, ["add-to-collection"])
        assert dry.exit_code == 0, dry.output
        assert "dry run" in dry.output
        assert d.posts == []
        applied = CliRunner().invoke(app, ["add-to-collection", "--apply"])
        assert applied.exit_code == 0, applied.output
    assert d.posts == [(77, 10)]
    assert "added 1" in applied.output
