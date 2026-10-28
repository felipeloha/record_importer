import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

from crate2discogs.db import BarcodeRow, Repo, connect
from crate2discogs.extract.base import ExtractionFailed, ExtractorUnavailable
from crate2discogs.extract.ollama import OllamaExtractor
from crate2discogs.models import Extraction, extraction_schema
from crate2discogs.pipeline import run_extract

URL = "http://ollama.test"
CHAT = f"{URL}/api/chat"

STICKER = {
    "photo_type": "shop_sticker",
    "artist": "SURGEON",
    "title": "BASICTONALVOCABULARY (2XLP)",
    "label": "Tresor",
    "catno": "TRESOR10073LP",
    "shop_code": "bw7 - j6*",
}


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


@pytest.fixture
def image(tmp_path: Path) -> Path:
    p = tmp_path / "img.jpg"
    p.write_bytes(b"\xff\xd8fakejpeg")
    return p


@pytest.fixture
def extractor() -> OllamaExtractor:
    return OllamaExtractor(URL, "qwen2.5vl:7b")


@respx.mock
def test_request_body(extractor: OllamaExtractor, image: Path) -> None:
    route = respx.post(CHAT).mock(return_value=_reply(json.dumps(STICKER)))
    extractor.extract(image)
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "qwen2.5vl:7b"
    assert body["options"]["temperature"] == 0
    assert body["format"] == extraction_schema()
    assert body["stream"] is False
    assert body["messages"][-1]["images"] == [base64.b64encode(image.read_bytes()).decode()]


def test_schema_has_no_refs_and_requires_every_field() -> None:
    schema = extraction_schema()
    assert "$ref" not in json.dumps(schema)
    assert set(schema["required"]) == set(Extraction.model_fields)
    assert "tracklist" not in schema["properties"]  # long, unused, and where the model looped


def test_postprocess_cleans_hints_and_country() -> None:
    from crate2discogs.models import postprocess

    e = postprocess(
        Extraction(
            title="IN WOOD, THAT (VINYL ONLY)", format_hints=["(VINYL ONLY)"], country="Made in Germany"
        )
    )
    assert (e.title, e.format_hints, e.country) == ("IN WOOD, THAT", ["Vinyl Only"], "Germany")
    e = postprocess(Extraction(title="PARADYGM SHIFT", format_hints=["(2LP)"]))
    assert (e.format_hints, e.disc_count) == (["2LP"], 2)


@respx.mock
def test_valid_json_parsed_and_hints_stripped(extractor: OllamaExtractor, image: Path) -> None:
    respx.post(CHAT).mock(return_value=_reply(json.dumps(STICKER)))
    e = extractor.extract(image).extraction
    assert e.title == "BASICTONALVOCABULARY"
    assert e.format_hints == ["2XLP"]
    assert e.disc_count == 2
    assert e.catno == "TRESOR10073LP"
    assert e.shop_code == "bw7 - j6*"


@respx.mock
def test_retry_after_invalid_json(extractor: OllamaExtractor, image: Path) -> None:
    route = respx.post(CHAT).mock(side_effect=[_reply("not json"), _reply(json.dumps(STICKER))])
    assert extractor.extract(image).extraction.label == "Tresor"
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert "ONLY a single JSON object" in second["messages"][-1]["content"]


@respx.mock
def test_two_invalid_responses_fail(extractor: OllamaExtractor, image: Path) -> None:
    respx.post(CHAT).mock(side_effect=[_reply("nope"), _reply('{"year": "x"}')])
    with pytest.raises(ExtractionFailed) as exc:
        extractor.extract(image)
    assert exc.value.raw == '{"year": "x"}'


@respx.mock
def test_ollama_down(extractor: OllamaExtractor, image: Path) -> None:
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ExtractorUnavailable, match="is `ollama serve` running"):
        extractor.extract(image)


@respx.mock
def test_model_missing(extractor: OllamaExtractor, image: Path) -> None:
    respx.post(CHAT).mock(return_value=httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ExtractorUnavailable, match="ollama pull"):
        extractor.extract(image)


class FakeExtractor:
    name = "fake"
    model: str | None = "m"

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[Path] = []
        self.fail_on = fail_on or set()

    def extract(self, image_path: Path):  # type: ignore[no-untyped-def]
        from crate2discogs.extract.base import ExtractResult

        self.calls.append(image_path)
        if image_path.name in self.fail_on:
            raise ExtractionFailed("bad json", raw="garbage")
        return ExtractResult(Extraction(catno="X1"), '{"catno":"X1"}')


def _decoded_repo(tmp_path: Path, barcodes: dict[int, list[BarcodeRow]] | None = None, n: int = 3) -> Repo:
    repo = Repo(connect(tmp_path / "t.db"))
    for i in range(n):
        pid, _ = repo.upsert_photo(f"{i:064d}", tmp_path / f"{i}.jpg", tmp_path / f"w{i}.jpg")
        repo.replace_barcodes(pid, (barcodes or {}).get(i, []))
        repo.set_stage(pid, "decoded")
    return repo


def test_pipeline_failed_photo_marked_and_others_continue(tmp_path: Path) -> None:
    repo = _decoded_repo(tmp_path)
    fake = FakeExtractor(fail_on={"w1.jpg"})
    assert run_extract(repo, fake) == 3
    by_name = {p.path.name: p for p in repo.all_photos()}
    assert by_name["0.jpg"].stage == "extracted"
    assert by_name["1.jpg"].stage == "extract_failed"
    assert by_name["2.jpg"].stage == "extracted"
    row = repo.get_extraction(by_name["1.jpg"].id)
    assert row is not None
    assert (row.error, row.raw_json, row.parsed_json) == ("bad json", "garbage", None)
    ok = repo.get_extraction(by_name["0.jpg"].id)
    assert ok is not None
    assert ok.parsed_json is not None
    assert Extraction.model_validate_json(ok.parsed_json).catno == "X1"


def test_pipeline_extracts_ean_photos_by_default(tmp_path: Path) -> None:
    ean = [BarcodeRow("EAN13", "4036909000014", True)]
    shop = [BarcodeRow("Code93", "BW7/J6", False)]
    repo = _decoded_repo(tmp_path, {0: ean, 1: shop}, n=2)
    fake = FakeExtractor()
    run_extract(repo, fake)
    assert [p.name for p in fake.calls] == ["w0.jpg", "w1.jpg"]

    (tmp_path / "b").mkdir()
    repo2 = _decoded_repo(tmp_path / "b", {0: ean, 1: shop}, n=2)
    fake2 = FakeExtractor()
    run_extract(repo2, fake2, always_extract=False)  # --skip-ean-photos
    assert [p.name for p in fake2.calls] == ["w1.jpg"]
    assert all(p.stage == "extracted" for p in repo2.all_photos())


def test_pipeline_stops_when_extractor_unavailable(tmp_path: Path) -> None:
    repo = _decoded_repo(tmp_path)

    class Down(FakeExtractor):
        def extract(self, image_path: Path):  # type: ignore[no-untyped-def]
            raise ExtractorUnavailable("is `ollama serve` running?")

    with pytest.raises(ExtractorUnavailable):
        run_extract(repo, Down())
    assert all(p.stage == "decoded" for p in repo.all_photos())


def test_postprocess_cleans_catno() -> None:
    from crate2discogs.models import postprocess

    e = postprocess(Extraction(catno="M-PLANT M.PM21", label="M-PLANT MUSIC"))
    assert e.catno == "M.PM21"
    e = postprocess(Extraction(catno="4250101497384", label="Kompakt"))
    assert (e.catno, e.ean_digits) == (None, "4250101497384")
