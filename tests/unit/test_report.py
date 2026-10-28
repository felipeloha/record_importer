import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crate2discogs.cli import app
from crate2discogs.db import BarcodeRow, CandidateRow, ExtractionRow, Repo, ResolutionRow, connect
from crate2discogs.models import Extraction
from crate2discogs.report import CSV_COLUMNS, export_csv, format_status, status_counts


def _repo(tmp_path: Path) -> Repo:
    repo = Repo(connect(tmp_path / "crate.db"))
    a, _ = repo.upsert_photo("a" * 64, tmp_path / "a.jpg", tmp_path / "wa.jpg")
    e = Extraction(artist="Mario Più & Mauro Picotto", title="Imperiale", catno="BXRCG 003-12")
    repo.save_extraction(a, ExtractionRow("fake", "m", None, e.model_dump_json(), None))
    repo.replace_barcodes(a, [BarcodeRow("EAN13", "4036909000014", True)])
    repo.replace_candidates(a, [CandidateRow(101, 0, 9.0, "{}", "{}")])
    repo.save_resolution(a, ResolutionRow("auto", 101, "auto"))
    b, _ = repo.upsert_photo("b" * 64, tmp_path / "b.jpg", tmp_path / "wb.jpg")
    repo.save_resolution(b, ResolutionRow("review", None, "auto"))
    c, _ = repo.upsert_photo("c" * 64, tmp_path / "c.jpg", tmp_path / "wc.jpg")
    repo.set_error(c, "extract: ollama timeout: read timed out")
    return repo


def test_csv_rows_and_url(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = tmp_path / "out.csv"
    assert export_csv(repo, out) == 3
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == CSV_COLUMNS
    assert len(rows) == 3
    assert rows[0]["discogs_url"] == "https://www.discogs.com/release/101"
    assert rows[0]["score"] == "9.0"
    assert rows[0]["ean"] == "4036909000014"
    assert rows[1]["discogs_url"] == ""
    assert rows[2]["error"].startswith("extract:")


def test_unicode_roundtrip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = tmp_path / "out.csv"
    export_csv(repo, out)
    assert "Più" in out.read_text(encoding="utf-8")


def test_summary_matches_db(tmp_path: Path) -> None:
    s = status_counts(_repo(tmp_path))
    assert s.stages["imported"] == 3
    assert s.outcomes == {"auto": 1, "review": 1}
    assert s.review_items == 1
    assert s.errors == 1
    assert s.failure_reasons == [("extract: ollama timeout: read", 1)]
    text = format_status(s)
    assert "review workload: 1 items" in text
    assert "1× extract: ollama timeout" in text


def test_cli_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "u")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "crate.db"))
    result = CliRunner().invoke(app, ["export", "--csv", str(tmp_path / "x.csv")])
    assert result.exit_code == 0, result.output
    assert "3 rows" in result.output
