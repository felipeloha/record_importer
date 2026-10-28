import json
from pathlib import Path
from typing import Any

from crate2discogs.db import BarcodeRow
from crate2discogs.models import Extraction
from crate2discogs.search import build_queries, run_search

FIX = json.loads((Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text())
EAN = [BarcodeRow("EAN13", "4036909000014", True)]


class FakeSearch:
    def __init__(self, answers: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers = answers or {}

    def __call__(self, **params: Any) -> dict[str, Any]:
        self.calls.append(params)
        key = json.dumps(
            {k: v for k, v in params.items() if k not in ("type", "format", "per_page")}, sort_keys=True
        )
        return {"results": self.answers.get(key, [])}


def _key(**q: Any) -> str:
    return json.dumps(q, sort_keys=True)


def _strip(q: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in q.items() if k not in ("type", "format", "per_page")}


def test_every_query_has_base_params() -> None:
    qs = build_queries(Extraction(catno="LOGO16RE", label="Logistic", artist="Robert Hood", title="X"), EAN)
    assert qs
    assert all(q["type"] == "release" and q["format"] == "Vinyl" for q in qs)


def test_valid_ean_first() -> None:
    qs = build_queries(Extraction(catno="BXRCG 003-12", label="BXR Club"), EAN)
    assert _strip(qs[0]) == {"barcode": "4036909000014"}
    assert _strip(qs[1]) == {"catno": "BXRCG 003-12", "label": "BXR Club"}


def test_catno_label_then_variants_in_order() -> None:
    qs = [_strip(q) for q in build_queries(Extraction(catno="LOGO16RE", label="Logistic"), [])]
    assert qs[0] == {"catno": "LOGO16RE", "label": "Logistic"}
    assert qs[1] == {"catno": "LOG016RE"}
    assert all(set(q) == {"catno"} for q in qs[1:])


def test_variants_keep_printed_punctuation() -> None:
    qs = [_strip(q) for q in build_queries(Extraction(catno="BXRCG 003-12"), [])]
    assert all(" " in q["catno"] and "-" in q["catno"] for q in qs)


def test_only_artist_title() -> None:
    qs = [_strip(q) for q in build_queries(Extraction(artist="Robert Hood", title="Paradygm Shift"), [])]
    assert qs == [
        {"artist": "Robert Hood", "release_title": "Paradygm Shift"},
        {"q": "Robert Hood Paradygm Shift"},
    ]


def test_budget_respected() -> None:
    fake = FakeSearch()
    e = Extraction(catno="SO1OB5O", label="L", artist="A", title="T")
    res = run_search(fake, e, [], budget=3)
    assert res.calls == 3
    assert len(fake.calls) == 3


def test_short_circuit_on_auto() -> None:
    fake = FakeSearch({_key(barcode="4036909000014"): [FIX["bxr_barcode"]]})
    res = run_search(fake, Extraction(catno="BXRCG 003-12", label="BXR Club"), EAN)
    assert res.calls == 1
    assert [c[0]["id"] for c in res.candidates] == [101]


def test_stops_after_catno_match_even_if_ambiguous() -> None:
    fake = FakeSearch({_key(catno="TRESOR10073LP", label="Tresor"): [FIX["surgeon_2lp"], FIX["surgeon_3lp"]]})
    e = Extraction(catno="TRESOR10073LP", label="Tresor", artist="Surgeon", title="Basictonalvocabulary")
    res = run_search(fake, e, [])
    assert res.calls == 1
    assert len(res.candidates) == 2


def test_continues_while_no_catno_match() -> None:
    # Discogs returns a near miss for the printed catno; the OCR variant then finds the real one.
    fake = FakeSearch(
        {
            _key(catno="LOGO16RE", label="Logistic"): [FIX["hood_other_catno"]],
            _key(catno="LOG016RE"): [FIX["hood_logistic"]],
        }
    )
    res = run_search(fake, Extraction(catno="LOGO16RE", label="Logistic"), [])
    assert res.calls == 2
    assert {c[0]["id"] for c in res.candidates} == {401, 402}


def test_runs_all_queries_when_nothing_found() -> None:
    fake = FakeSearch()
    e = Extraction(catno="DKMNTL 031", label="Dekmantel", artist="Robert Hood", title="Paradygm Shift")
    res = run_search(fake, e, [], budget=100)
    assert res.calls == len(build_queries(e, []))


def test_shop_code_never_in_query() -> None:
    e = Extraction(catno="c3f - ic+", label="Perlon", artist="Margaret Dygas", shop_code="c3f - ic+")
    for q in build_queries(e, []):
        assert "c3f" not in json.dumps(q)
    e2 = Extraction(catno="bw7 - j6*", title="BASICTONALVOCABULARY")
    for q in build_queries(e2, []):
        assert "bw7" not in json.dumps(q)


def test_ean_digits_used_only_if_checksum_valid() -> None:
    assert _strip(build_queries(Extraction(ean_digits="4250101497384"), [])[0]) == {
        "barcode": "4250101497384"
    }
    assert build_queries(Extraction(ean_digits="4250101497385"), []) == []


def test_shop_barcode_never_queried() -> None:
    assert build_queries(None, [BarcodeRow("Code93", "4250101497384", False)]) == []
