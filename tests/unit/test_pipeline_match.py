import json
from pathlib import Path
from typing import Any

from crate2discogs.db import BarcodeRow, ExtractionRow, Repo, connect
from crate2discogs.models import Extraction
from crate2discogs.pipeline import run_match

FIX = json.loads((Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text())


def _photo(repo: Repo, i: int, stage: str, extraction: Extraction | None, barcodes: list[BarcodeRow]) -> int:
    pid, _ = repo.upsert_photo(f"{i:064d}", Path(f"/{i}.jpg"), Path(f"/w{i}.jpg"))
    repo.set_stage(pid, "decoded")
    repo.set_stage(pid, stage)  # type: ignore[arg-type]
    repo.replace_barcodes(pid, barcodes)
    if extraction is not None:
        repo.save_extraction(pid, ExtractionRow("fake", "m", None, extraction.model_dump_json(), None))
    return pid


def test_match_stores_ranked_candidates(tmp_path: Path) -> None:
    repo = Repo(connect(tmp_path / "t.db"))
    calls: list[dict[str, Any]] = []

    def search(**params: Any) -> dict[str, Any]:
        calls.append(params)
        return {"results": [FIX["bxr_barcode"]] if "barcode" in params else []}

    ean = [BarcodeRow("EAN13", "4036909000014", True)]
    a = _photo(repo, 1, "extracted", None, ean)  # EAN photo, extraction skipped
    b = _photo(repo, 2, "extract_failed", None, [])  # nothing known: no queries
    assert run_match(repo, search) == 2
    assert len(calls) == 1
    (cand,) = repo.get_candidates(a)
    assert cand.release_id == 101
    assert json.loads(cand.source_query)["barcode"] == "4036909000014"
    assert repo.get_candidates(b) == []
    assert all(p.stage == "matched" for p in repo.all_photos())
