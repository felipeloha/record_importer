import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from crate2discogs.db import CandidateRow, ExtractionRow, Outcome, Repo, ResolutionRow, connect
from crate2discogs.dedupe import groups
from crate2discogs.discogs import DiscogsNotFound
from crate2discogs.models import Extraction
from crate2discogs.review.app import HOST, InvalidReleaseInput, create_app, parse_release_input

FIX = json.loads((Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text())


class FakeDiscogs:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.searches: list[dict[str, Any]] = []
        self.search_results: list[dict[str, Any]] = []

    def search(self, **params: Any) -> dict[str, Any]:
        self.searches.append(params)
        return {"results": self.search_results}

    def release(self, release_id: int) -> dict[str, Any]:
        self.calls.append(release_id)
        if release_id == 404:
            raise DiscogsNotFound("nope")
        return {"id": release_id, "title": "Pasted", "artists": [{"name": "Someone"}], "labels": []}


def _photo(
    repo: Repo, tmp: Path, i: int, outcome: Outcome, release_id: int | None, cands: list[str], catno: str = ""
) -> int:
    work = tmp / f"w{i}.jpg"
    Image.new("RGB", (20, 20), "white").save(work)
    pid, _ = repo.upsert_photo(f"{i:064d}", tmp / f"{i}.jpg", work)
    for stage in ("decoded", "extracted", "matched", "resolved"):
        repo.set_stage(pid, stage)  # type: ignore[arg-type]
    repo.save_extraction(
        pid,
        ExtractionRow(
            "fake",
            "m",
            None,
            Extraction(catno=catno or f"CAT{i}", artist="Artist", label="Dekmantel").model_dump_json(),
            None,
        ),
    )
    repo.replace_candidates(
        pid, [CandidateRow(FIX[c]["id"], r, 4.0 - r, "{}", json.dumps(FIX[c])) for r, c in enumerate(cands)]
    )
    repo.save_resolution(pid, ResolutionRow(outcome, release_id, "auto"))
    return pid


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]:
    repo = Repo(connect(tmp_path / "t.db"))
    ids = {
        "review": _photo(repo, tmp_path, 1, "review", None, ["surgeon_2lp", "surgeon_3lp"]),
        "not_found": _photo(repo, tmp_path, 2, "not_found", None, []),
        "auto_single": _photo(repo, tmp_path, 3, "auto", 201, ["dekmantel_031"], catno="DKMNTL 031"),
        "dup_a": _photo(repo, tmp_path, 4, "auto", 101, ["bxr_barcode"]),
        "dup_b": _photo(repo, tmp_path, 5, "auto", 101, ["bxr_barcode"]),
    }
    fake = FakeDiscogs()
    client = TestClient(create_app(repo, fake, margin=1.0))
    return repo, client, fake, ids


def _sections(html: str) -> tuple[str, str]:
    main, _, collapsed = html.partition('<section id="collapsed">')
    return main, collapsed


def test_queue_sections(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    _, client, _, ids = setup
    main, collapsed = _sections(client.get("/").text)
    assert f'href="/photos/{ids["review"]}"' in main
    assert f'href="/photos/{ids["not_found"]}"' in main
    assert f'href="/photos/{ids["dup_a"]}"' in main  # multi-photo group, even though auto
    assert f'href="/photos/{ids["auto_single"]}"' not in main
    assert f'href="/photos/{ids["auto_single"]}"' in collapsed


def test_pick_stores_user_resolution(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, _, ids = setup
    r = client.post(f"/photos/{ids['review']}/pick", data={"release_id": 301}, follow_redirects=False)
    assert r.status_code == 303
    res = repo.get_resolution(ids["review"])
    assert res is not None and (res.release_id, res.resolved_by) == (301, "user")
    main, collapsed = _sections(client.get("/").text)
    assert f'href="/photos/{ids["review"]}"' not in main
    assert f'href="/photos/{ids["review"]}"' in collapsed


def test_pick_redirects_to_next_pending(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    _, client, _, ids = setup
    r = client.post(f"/photos/{ids['review']}/pick", data={"release_id": 301}, follow_redirects=False)
    assert r.headers["location"] == f"/photos/{ids['not_found']}"


@pytest.mark.parametrize(
    ("value", "rid"),
    [
        ("https://www.discogs.com/release/123-Foo", 123),
        ("https://www.discogs.com/de/release/456-Bar-Baz", 456),
        ("789", 789),
        ("[r42]", 42),
    ],
)
def test_parse_release_input(value: str, rid: int) -> None:
    assert parse_release_input(value) == rid


@pytest.mark.parametrize("value", ["https://www.discogs.com/master/123-Foo", "abc", ""])
def test_parse_release_input_rejects(value: str) -> None:
    with pytest.raises(InvalidReleaseInput):
        parse_release_input(value)


def test_paste_url_validates_and_saves(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, fake, ids = setup
    r = client.post(
        f"/photos/{ids['not_found']}/paste",
        data={"value": "https://www.discogs.com/release/123-Foo"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert fake.calls == [123]
    res = repo.get_resolution(ids["not_found"])
    assert res is not None and (res.release_id, res.resolved_by) == (123, "user")


@pytest.mark.parametrize("value", ["404", "https://www.discogs.com/master/99-X", "nonsense"])
def test_paste_invalid_no_write(
    setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]], value: str
) -> None:
    repo, client, _, ids = setup
    r = client.post(f"/photos/{ids['not_found']}/paste", data={"value": value})
    assert r.status_code == 400
    assert 'class="box error"' in r.text
    res = repo.get_resolution(ids["not_found"])
    assert res is not None and res.resolved_by == "auto"


def test_skip_excluded_from_plan(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, _, ids = setup
    client.post(f"/photos/{ids['auto_single']}/skip")
    res = repo.get_resolution(ids["auto_single"])
    assert res is not None and res.outcome == "skip"
    assert 201 not in [g.release_id for g in groups(repo)]


def test_copies_stored(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, _, _ = setup
    r = client.post("/releases/101/copies", data={"copies": 2, "back": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_copies(101) == 2
    assert client.post("/releases/101/copies", data={"copies": 0}).status_code == 422


def test_copies_back_url_stays_local(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    _, client, _, _ = setup
    r = client.post(
        "/releases/101/copies", data={"copies": 1, "back": "//evil.example"}, follow_redirects=False
    )
    assert r.headers["location"] == "/"


def test_override_auto_and_undo(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, _, ids = setup
    pid = ids["auto_single"]
    client.post(f"/photos/{pid}/pick", data={"release_id": 999})
    res = repo.get_resolution(pid)
    assert res is not None and (res.release_id, res.resolved_by) == (999, "user")
    client.post(f"/photos/{pid}/undo")
    res = repo.get_resolution(pid)
    assert res is not None and (res.outcome, res.release_id, res.resolved_by) == ("auto", 201, "auto")


def test_photo_page_shows_candidates(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    _, client, _, ids = setup
    html = client.get(f"/photos/{ids['review']}").text
    assert "Surgeon – Basictonalvocabulary" in html
    assert "https://www.discogs.com/release/301" in html
    assert "TRESOR10073LP" in html


def test_photo_image_by_id_only(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    _, client, _, ids = setup
    r = client.get(f"/photos/{ids['review']}/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert client.get("/photos/9999/image").status_code == 404
    assert client.get("/photos/9999").status_code == 404
    assert client.get("/photos/..%2F..%2Fetc%2Fpasswd/image").status_code in (404, 422)


def test_added_photo_cannot_change(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, _, ids = setup
    repo.set_stage(ids["auto_single"], "added")
    assert client.post(f"/photos/{ids['auto_single']}/skip").status_code == 409


def test_binds_localhost_only() -> None:
    assert HOST == "127.0.0.1"


def test_parallel_requests_share_db_safely(
    setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]],
) -> None:
    # The queue page makes the browser load all thumbnails at once (regression: sqlite3.InterfaceError).
    from concurrent.futures import ThreadPoolExecutor

    _, client, _, ids = setup
    urls = [f"/photos/{pid}/image" for pid in ids.values()] * 10 + ["/"] * 10
    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert set(codes) == {200}


def test_search_box_and_links(setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]]) -> None:
    repo, client, fake, ids = setup
    fake.search_results = [
        {"id": 555, "title": "Sound Stream - Bass Affairs", "label": ["Sound Stream"], "catno": "SST 07"}
    ]
    html = client.get(f"/photos/{ids['not_found']}").text
    assert "https://www.google.com/search?q=discogs+Artist+CAT2" in html
    assert 'name="q"' in html
    html = client.get(f"/photos/{ids['not_found']}", params={"q": "sound stream bass affairs"}).text
    assert fake.searches == [{"q": "sound stream bass affairs", "type": "release", "per_page": 10}]
    assert "Sound Stream – Bass Affairs" in html
    client.post(f"/photos/{ids['not_found']}/pick", data={"release_id": 555})
    res = repo.get_resolution(ids["not_found"])
    assert res is not None and (res.release_id, res.resolved_by) == (555, "user")


def test_decided_sorted_skip_then_not_found(
    setup: tuple[Repo, TestClient, FakeDiscogs, dict[str, int]],
) -> None:
    from crate2discogs.review.app import build_queue

    repo, client, _, ids = setup
    client.post(f"/photos/{ids['review']}/pick", data={"release_id": 301})  # review → picked
    client.post(f"/photos/{ids['not_found']}/pick", data={"release_id": 77})  # not_found → picked
    client.post(f"/photos/{ids['auto_single']}/skip")
    order = [i.photo.id for i in build_queue(repo).decided]
    assert order == [ids["auto_single"], ids["not_found"], ids["review"]]
