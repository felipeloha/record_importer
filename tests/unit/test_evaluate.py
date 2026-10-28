from pathlib import Path

import pytest
from PIL import Image

from crate2discogs.db import BarcodeRow, Repo, connect
from crate2discogs.evaluate import (
    FIELDS,
    ExpectedEntry,
    field_accuracy,
    format_report,
    load_expected,
    predicted_fields,
    run_eval,
)
from crate2discogs.extract.base import ExtractionFailed, ExtractResult
from crate2discogs.models import Extraction

EXPECTED = Path(__file__).parents[1] / "fixtures" / "expected.yaml"


def test_fixture_yaml_loads_and_files_exist() -> None:
    entries = load_expected(EXPECTED)
    assert len(entries) == 10
    assert all(e.file.is_file() for e in entries)
    dups = [e for e in entries if e.duplicate_of]
    assert len(dups) == 1
    assert {e.ean for e in entries if e.ean} == {"4250101497384", "4036909000014"}


def test_yaml_rejects_unknown_key(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    f = tmp_path / "e.yaml"
    f.write_text("- file: a.jpg\n  photo_type: ean\n  catnum: X\n")
    with pytest.raises(ValueError, match="catnum"):
        load_expected(f)


def test_yaml_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "e.yaml"
    f.write_text("- file: nope.jpg\n  photo_type: ean\n")
    with pytest.raises(ValueError, match="file not found"):
        load_expected(f)


def _exp(**kw: object) -> dict[str, object]:
    return {f: None for f in FIELDS} | kw


def test_field_accuracy_with_normalisation_and_nulls() -> None:
    pairs = [
        (
            _exp(artist="Mario Più & Mauro Picotto", catno="BXRCG 003-12", ean="4036909000014"),
            _exp(artist="MARIO PIU' and MAURO PICOTTO", catno="BXRCG00312", ean="4036909000014"),
        ),
        (_exp(catno="LOG016RE", disc_count=2), _exp(catno="LOGO16RE", disc_count=2)),
        (_exp(title=None), _exp(title="Something")),
    ]
    s = field_accuracy(pairs)
    assert (s["artist"].correct, s["artist"].total) == (3, 3)
    assert (s["catno"].correct, s["catno"].total) == (2, 3)
    assert s["ean"].correct == 3
    assert s["disc_count"].correct == 3
    assert s["title"].correct == 2
    assert s["photo_type"].accuracy == 1.0


def test_predicted_ean_prefers_decoder_then_valid_digits() -> None:
    decoded = [BarcodeRow("EAN13", "4036909000014", True)]
    assert predicted_fields(Extraction(ean_digits="4250101497384"), decoded)["ean"] == "4036909000014"
    assert predicted_fields(Extraction(ean_digits="4250101497384"), [])["ean"] == "4250101497384"
    assert predicted_fields(Extraction(ean_digits="4250101497385"), [])["ean"] is None
    shop = [BarcodeRow("Code93", "BW7/J6", False)]
    assert predicted_fields(None, shop)["ean"] is None


class Fake:
    name = "fake"
    model: str | None = "m"

    def __init__(self, by_size: dict[int, Extraction | None]) -> None:
        self.by_size = by_size
        self.calls = 0

    def extract(self, image_path: Path) -> ExtractResult:
        self.calls += 1
        with Image.open(image_path) as im:
            e = self.by_size[im.width]
        if e is None:
            raise ExtractionFailed("bad json")
        return ExtractResult(e, e.model_dump_json())


def test_run_eval_scores_and_caches(tmp_path: Path) -> None:
    for w in (10, 11):
        Image.new("RGB", (w, 10), "white").save(tmp_path / f"{w}.png")
    entries = [
        ExpectedEntry(file=tmp_path / "10.png", photo_type="shop_sticker", catno="DKMNTL 031"),
        ExpectedEntry(file=tmp_path / "11.png", photo_type="ean", ean="4250101497384"),
    ]
    fake = Fake({10: Extraction(photo_type="shop_sticker", catno="DKMNTL031"), 11: None})
    repo = Repo(connect(tmp_path / "eval.db"))
    report = run_eval(repo, entries, fake, tmp_path / "work", 1600)
    assert report.scores["catno"].correct == 2
    assert report.scores["ean"].correct == 1
    assert report.seconds_per_photo is not None
    failed = next(p for p in report.photos if p.file.name == "11.png")
    assert failed.error == "bad json"
    assert "11.png" in format_report(report)

    again = run_eval(repo, entries, fake, tmp_path / "work", 1600)
    assert fake.calls == 2  # cached, not re-extracted
    assert again.seconds_per_photo is None


def test_run_eval_end_to_end_verdicts(tmp_path: Path) -> None:
    import json

    fix = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "discogs" / "search_candidates.json").read_text()
    )
    for w in (10, 11, 12):
        Image.new("RGB", (w, 10), "white").save(tmp_path / f"{w}.png")
    entries = [
        ExpectedEntry(file=tmp_path / "10.png", photo_type="shop_sticker", expected_release_id=201),
        ExpectedEntry(file=tmp_path / "11.png", photo_type="shop_sticker", expected_release_id=302),
        ExpectedEntry(
            file=tmp_path / "12.png",
            photo_type="shop_sticker",
            expected_release_id=302,
            duplicate_of=tmp_path / "11.png",
        ),
    ]
    surgeon = Extraction(catno="TRESOR10073LP", label="Tresor")
    fake = Fake({10: Extraction(catno="DKMNTL 031", label="Dekmantel"), 11: surgeon, 12: surgeon})

    def search(**params: object) -> dict[str, object]:
        if params.get("catno") == "DKMNTL 031":
            return {"results": [fix["dekmantel_031"]]}
        if params.get("catno") == "TRESOR10073LP":
            return {"results": [fix["surgeon_2lp"], fix["surgeon_3lp"]]}
        return {"results": []}

    report = run_eval(Repo(connect(tmp_path / "e.db")), entries, fake, tmp_path / "w", 1600, search=search)
    verdicts = {p.file.name: p.verdict for p in report.photos}
    assert verdicts == {"10.png": "correct_auto", "11.png": "review_ok", "12.png": "review_ok"}
    text = format_report(report)
    assert "wrong_auto" in text
    assert "duplicate 12.png ~ 11.png" in text


@pytest.mark.live
def test_live_ollama_catno_accuracy(tmp_path: Path) -> None:
    from crate2discogs.config import load_settings
    from crate2discogs.extract import make_extractor

    settings = load_settings()
    entries = load_expected(EXPECTED)
    report = run_eval(
        Repo(connect(tmp_path / "eval.db")), entries, make_extractor(settings), tmp_path, settings.max_edge
    )
    print(format_report(report))
    assert report.scores["catno"].accuracy >= 0.9  # AC-3
    kompakt = next(p for p in report.photos if p.file.name.startswith("a236e6b3"))
    assert kompakt.predicted["ean"] == "4250101497384"  # AC-1 fallback via ean_digits
