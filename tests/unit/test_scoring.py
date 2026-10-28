import json
from pathlib import Path
from typing import Any

import pytest

from crate2discogs.db import BarcodeRow
from crate2discogs.models import Extraction
from crate2discogs.scoring import WEIGHTS, rank, resolve, score

FIX = json.loads((Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text())
MARGIN = 1.0


def _c(name: str, **override: Any) -> dict[str, Any]:
    return FIX[name] | {**override}


def _resolve(cands: list[dict[str, Any]], e: Extraction | None, barcodes: list[BarcodeRow] | None = None):  # type: ignore[no-untyped-def]
    return resolve(rank([(c, {}) for c in cands], e, barcodes or []), MARGIN)


EAN = [BarcodeRow("EAN13", "4036909000014", True)]


def test_single_barcode_hit_auto() -> None:
    r = _resolve([_c("bxr_barcode")], None, EAN)
    assert (r.outcome, r.release_id) == ("auto", 101)


def test_barcode_from_valid_ean_digits() -> None:
    r = _resolve([_c("bxr_barcode")], Extraction(ean_digits="4036909000014"))
    assert r.outcome == "auto"
    r = _resolve([_c("bxr_barcode")], Extraction(ean_digits="4036909000015"))
    assert r.outcome == "review"


def test_shop_barcode_never_matches() -> None:
    shop = [BarcodeRow("Code93", "4036909000014", False)]
    assert _resolve([_c("bxr_barcode")], None, shop).outcome == "review"


def test_catno_and_label_single_result_auto() -> None:
    e = Extraction(catno="DKMNTL 031", label="Dekmantel")
    r = _resolve([_c("dekmantel_031")], e)
    assert (r.outcome, r.release_id) == ("auto", 201)
    assert r.ranked[0].matched == ("catno", "label")


def test_catno_ocr_variant_counts_as_match() -> None:
    e = Extraction(catno="LOGO16RE", artist="Robert Hood", title="Apartement Zero", label="Logistic")
    r = _resolve([_c("hood_logistic")], e)
    assert (r.outcome, r.release_id) == ("auto", 401)


def test_disc_count_breaks_tie_between_pressings() -> None:
    e = Extraction(catno="TRESOR10073LP", label="Tresor", disc_count=2)
    r = _resolve([_c("surgeon_3lp"), _c("surgeon_2lp")], e)
    assert (r.outcome, r.release_id) == ("auto", 301)


def test_indistinguishable_pressings_review() -> None:
    e = Extraction(catno="TRESOR10073LP", label="Tresor")
    r = _resolve([_c("surgeon_3lp"), _c("surgeon_2lp")], e)
    assert r.outcome == "review"
    assert r.release_id is None
    assert {s.release_id for s in r.ranked[:3]} == {301, 302}


def test_same_barcode_two_pressings_review() -> None:
    other = _c("bxr_barcode", id=102, year="2005")
    r = _resolve([_c("bxr_barcode"), other], None, EAN)
    assert r.outcome == "review"


def test_fuzzy_artist_title_only_review() -> None:
    e = Extraction(artist="Robert Hood", title="Paradygm Shift Volume 1")
    r = _resolve([_c("dekmantel_031")], e)
    assert r.outcome == "review"
    assert set(r.ranked[0].matched) == {"artist", "title"}


def test_no_candidates_not_found() -> None:
    assert _resolve([], Extraction(catno="X")).outcome == "not_found"


def test_duplicate_release_from_two_queries_counted_once() -> None:
    e = Extraction(catno="DKMNTL 031")
    ranked = rank([(_c("dekmantel_031"), {"catno": "a"}), (_c("dekmantel_031"), {"catno": "b"})], e, [])
    assert len(ranked) == 1
    assert resolve(ranked, MARGIN).outcome == "auto"


@pytest.mark.parametrize("extra", list(WEIGHTS))
def test_monotonic_adding_matching_field_never_lowers(extra: str) -> None:
    cand = _c("bxr_barcode")
    base = Extraction()
    fields: dict[str, Any] = {
        "barcode": {"ean_digits": "4036909000014"},
        "catno": {"catno": "BXRCG 003-12"},
        "artist": {"artist": "Mario Piu & Mauro Picotto"},
        "title": {"title": "Imperiale"},
        "label": {"label": "BXR Club"},
        "disc_count": {"disc_count": None},
        "year": {"year": 1999},
        "country": {"country": "Germany"},
    }
    before = score(cand, base, []).score
    after = score(cand, base.model_copy(update=fields[extra]), []).score
    assert after >= before
    if extra != "disc_count":
        assert after > before


def test_regression_catno_differs_never_auto() -> None:
    # Artist/title/label all match but the catno key differs: never auto (AC-5).
    e = Extraction(catno="LOG016RE", artist="Robert Hood", title="Apartement Zero", label="Logistic")
    r = _resolve([_c("hood_other_catno")], e)
    assert r.outcome == "review"
    assert not r.ranked[0].hard_match


def test_label_suffix_and_case_ignored() -> None:
    cand = _c("dekmantel_031", label=["Dekmantel (2)"])
    assert "label" in score(cand, Extraction(label="DEKMANTEL"), []).matched
