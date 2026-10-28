import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from crate2discogs.cli import app
from crate2discogs.db import BarcodeRow, Repo, connect
from crate2discogs.extract.base import ExtractResult
from crate2discogs.models import Extraction
from crate2discogs.pipeline import identify
from crate2discogs.report import format_status, status_counts

FIX = json.loads((Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text())

# photo index -> (extraction, search answer by catno)
PHOTOS = {
    0: Extraction(catno="DKMNTL 031", label="Dekmantel"),
    1: Extraction(catno="TRESOR10073LP", label="Tresor", disc_count=2),
    2: Extraction(catno="LOGO16RE", label="Logistic", artist="Robert Hood", title="Apartement Zero"),
    3: Extraction(catno="TRESOR10073LP", label="Tresor"),
    4: Extraction(artist="Nobody", title="Nothing"),
}
RESULTS: dict[str, list[dict[str, Any]]] = {
    "DKMNTL 031": [FIX["dekmantel_031"]],
    "TRESOR10073LP": [FIX["surgeon_2lp"], FIX["surgeon_3lp"]],
    "LOG016RE": [FIX["hood_logistic"]],
}


class FakeExtractor:
    name = "fake"
    model: str | None = "m"

    def __init__(self, crash_on: int | None = None) -> None:
        self.calls: list[int] = []
        self.crash_on = crash_on

    def extract(self, image_path: Path) -> ExtractResult:
        i = int(image_path.stem[1:])
        if i == self.crash_on:
            raise RuntimeError("simulated crash")
        self.calls.append(i)
        e = PHOTOS[i]
        return ExtractResult(e, e.model_dump_json())


class FakeSearch:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **params: Any) -> dict[str, Any]:
        self.calls += 1
        return {"results": RESULTS.get(params.get("catno", ""), [])}


def _repo(tmp_path: Path, n: int = 5) -> Repo:
    repo = Repo(connect(tmp_path / "t.db"))
    for i in range(n):
        repo.upsert_photo(f"{i:064d}", tmp_path / f"{i}.jpg", tmp_path / f"w{i}.jpg")
    return repo


def _run(repo: Repo, ext: FakeExtractor, search: FakeSearch, **kw: Any) -> dict[str, int]:
    return identify(  # type: ignore[return-value]
        repo,
        extractor=lambda: ext,
        search=lambda: search,
        margin=1.0,
        decoder=lambda p: [BarcodeRow("Code93", "SHOP", False)],
        **kw,
    )


def test_end_to_end_all_resolved(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, FakeExtractor(), FakeSearch())
    assert all(p.stage == "resolved" for p in repo.all_photos())
    outcomes = {p.id: repo.get_resolution(p.id) for p in repo.all_photos()}
    got = {pid: (r.outcome, r.release_id) for pid, r in outcomes.items() if r}
    assert got == {
        1: ("auto", 201),
        2: ("auto", 301),
        3: ("auto", 401),
        4: ("review", None),
        5: ("not_found", None),
    }


def test_crash_mid_extract_resumes_without_redoing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = FakeExtractor(crash_on=2)
    with pytest.raises(RuntimeError):
        _run(repo, first, FakeSearch())
    assert first.calls == [0, 1]
    second = FakeExtractor()
    _run(repo, second, FakeSearch())
    assert second.calls == [2, 3, 4]
    assert all(p.stage == "resolved" for p in repo.all_photos())


def test_limit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ext = FakeExtractor()
    done = _run(repo, ext, FakeSearch(), limit=2)
    assert done == {"decode": 2, "extract": 2, "match": 2, "resolve": 2}
    assert ext.calls == [0, 1]
    assert sum(p.stage == "resolved" for p in repo.all_photos()) == 2


def test_stage_match_never_calls_extractor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, FakeExtractor(), FakeSearch(), step="decode")

    def boom() -> FakeExtractor:
        raise AssertionError("extractor must not be created")

    search = FakeSearch()
    identify(repo, extractor=boom, search=lambda: search, margin=1.0, step="match")
    assert search.calls == 0  # nothing is at extracted yet
    assert all(p.stage == "decoded" for p in repo.all_photos())


def test_force_resolve_no_discogs_calls(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, FakeExtractor(), FakeSearch())

    def no_search() -> FakeSearch:
        raise AssertionError("resolve must not search")

    identify(repo, extractor=FakeExtractor, search=no_search, margin=100.0, step="resolve", force=True)
    r = repo.get_resolution(1)
    assert r is not None and r.outcome == "auto"  # single candidate: no runner-up, margin irrelevant
    r = repo.get_resolution(2)
    assert r is not None and r.outcome == "review"


def test_progress_ticks(tmp_path: Path) -> None:
    repo = _repo(tmp_path, n=3)
    ticks: dict[str, int] = {}

    def progress(name: str, total: int):  # type: ignore[no-untyped-def]
        assert total == 3
        ticks[name] = 0

        def tick() -> None:
            ticks[name] += 1

        return tick

    _run(repo, FakeExtractor(), FakeSearch(), progress=progress)
    assert ticks == {"decode": 3, "extract": 3, "match": 3, "resolve": 3}


def test_status_matches_db(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, FakeExtractor(), FakeSearch(), limit=3)
    s = status_counts(repo)
    assert s.stages["resolved"] == 3
    assert s.stages["imported"] == 2
    assert s.outcomes == {"auto": 3}
    text = format_status(s)
    assert "resolved" in text and "auto" in text


def test_cli_identify_stage_decode_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "u")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "crate.db"))
    monkeypatch.chdir(tmp_path)
    from PIL import Image

    (tmp_path / "in").mkdir()
    Image.new("RGB", (30, 30), "white").save(tmp_path / "in" / "a.png")
    runner = CliRunner()
    assert runner.invoke(app, ["import", str(tmp_path / "in")]).exit_code == 0
    result = runner.invoke(app, ["identify", "--stage", "decode"])
    assert result.exit_code == 0, result.output
    assert "decode=1" in result.output
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0
    assert "decoded" in status.output


def test_retry_skipped_flow(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(repo, FakeExtractor(), FakeSearch())
    from crate2discogs.db import ResolutionRow

    repo.save_resolution(1, ResolutionRow("skip", None, "user"))
    repo.save_resolution(2, ResolutionRow("review", 999, "user"))
    assert repo.clear_user_skips() == 1
    ext = FakeExtractor()
    search = FakeSearch()
    _run(repo, ext, search, force=True)
    assert ext.calls == []  # no re-extraction
    assert search.calls > 0  # searched again
    r1, r2 = repo.get_resolution(1), repo.get_resolution(2)
    assert r1 is not None and (r1.outcome, r1.release_id, r1.resolved_by) == ("auto", 201, "auto")
    assert r2 is not None and (r2.release_id, r2.resolved_by) == (999, "user")


def test_force_retries_failed_extraction(tmp_path: Path) -> None:
    from crate2discogs.extract.base import ExtractionFailed

    repo = _repo(tmp_path, n=3)

    class Flaky(FakeExtractor):
        def extract(self, image_path: Path) -> ExtractResult:
            if image_path.stem == "w1":
                raise ExtractionFailed("invalid JSON after retry", raw="{ truncated")
            return super().extract(image_path)

    _run(repo, Flaky(), FakeSearch())
    assert repo.get_photo(2).stage == "resolved"  # type: ignore[union-attr]
    row = repo.get_extraction(2)
    assert row is not None and row.error

    plain = FakeExtractor()
    _run(repo, plain, FakeSearch())
    assert plain.calls == []  # without --force nothing is retried

    retry = FakeExtractor()
    _run(repo, retry, FakeSearch(), force=True)
    assert retry.calls == [1]  # only the failed one is extracted again
    row = repo.get_extraction(2)
    assert row is not None and row.error is None
    r = repo.get_resolution(2)
    assert r is not None and (r.outcome, r.release_id) == ("auto", 301)
