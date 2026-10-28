import json
from pathlib import Path

from crate2discogs.db import CandidateRow, ExtractionRow, Repo, ResolutionRow, connect
from crate2discogs.models import Extraction
from crate2discogs.pipeline import run_resolve

FIX = json.loads((Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text())


def _matched(repo: Repo, i: int, extraction: Extraction, cands: list[str]) -> int:
    pid, _ = repo.upsert_photo(f"{i:064d}", Path(f"/{i}.jpg"), Path(f"/w{i}.jpg"))
    for stage in ("decoded", "extracted", "matched"):
        repo.set_stage(pid, stage)  # type: ignore[arg-type]
    repo.save_extraction(pid, ExtractionRow("fake", "m", None, extraction.model_dump_json(), None))
    repo.replace_candidates(
        pid, [CandidateRow(FIX[c]["id"], r, 0.0, '{"q": 1}', json.dumps(FIX[c])) for r, c in enumerate(cands)]
    )
    return pid


def test_resolve_auto_and_review(tmp_path: Path) -> None:
    repo = Repo(connect(tmp_path / "t.db"))
    a = _matched(repo, 1, Extraction(catno="DKMNTL 031", label="Dekmantel"), ["dekmantel_031"])
    b = _matched(repo, 2, Extraction(catno="TRESOR10073LP", label="Tresor"), ["surgeon_3lp", "surgeon_2lp"])
    c = _matched(repo, 3, Extraction(catno="NOPE"), [])
    assert run_resolve(repo, margin=1.0) == 3
    ra, rb, rc = (repo.get_resolution(x) for x in (a, b, c))
    assert ra is not None and (ra.outcome, ra.release_id, ra.resolved_by) == ("auto", 201, "auto")
    assert rb is not None and rb.outcome == "review"
    assert rc is not None and rc.outcome == "not_found"
    assert all(p.stage == "resolved" for p in repo.all_photos())
    top = repo.get_candidates(a)[0]
    assert (top.rank, top.score) == (0, 5.0)


def test_force_rescores_without_touching_user_choice(tmp_path: Path) -> None:
    repo = Repo(connect(tmp_path / "t.db"))
    a = _matched(
        repo,
        1,
        Extraction(catno="TRESOR10073LP", label="Tresor", disc_count=2),
        ["surgeon_3lp", "surgeon_2lp"],
    )
    b = _matched(
        repo,
        2,
        Extraction(catno="TRESOR10073LP", label="Tresor", disc_count=2),
        ["surgeon_3lp", "surgeon_2lp"],
    )
    run_resolve(repo, margin=1.0)
    ra = repo.get_resolution(a)
    assert ra is not None and ra.outcome == "auto"
    repo.save_resolution(b, ResolutionRow("skip", None, "user"))

    assert run_resolve(repo, margin=5.0) == 0  # without --force resolved photos are left alone
    assert run_resolve(repo, margin=5.0, force=True) == 2
    ra, rb = repo.get_resolution(a), repo.get_resolution(b)
    assert ra is not None and ra.outcome == "review"
    assert rb is not None and (rb.outcome, rb.resolved_by) == ("skip", "user")
