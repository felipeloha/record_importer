from io import BytesIO
from pathlib import Path

import barcode
import pytest
from barcode.writer import ImageWriter
from PIL import Image

from crate2discogs.barcodes import decode, ean_checksum_valid, upce_to_upca, valid_eans

FIXTURES = Path(__file__).parents[1] / "fixtures" / "photos"


@pytest.mark.parametrize(
    ("digits", "ok"),
    [
        ("4250101497384", True),
        ("4036909000014", True),
        ("4250101497385", False),
        ("0123", False),
        ("036000291452", True),
        ("96385074", True),
        ("96385075", False),
        ("", False),
        ("42501014973a4", False),
    ],
)
def test_checksum(digits: str, ok: bool) -> None:
    assert ean_checksum_valid(digits) is ok


def test_upce_expansion() -> None:
    assert upce_to_upca("04252614") == "042100005264"


def _render(kind: str, data: str, path: Path, rotate: int = 0) -> Path:
    buf = BytesIO()
    barcode.get(kind, data, writer=ImageWriter()).write(buf, options={"module_width": 0.4, "quiet_zone": 8})
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor="white")
    img.save(path)
    return path


def test_synthetic_ean13(tmp_path: Path) -> None:
    (b,) = decode(_render("ean13", "425010149738", tmp_path / "e.png"))
    assert (b.symbology, b.value, b.valid) == ("EAN13", "4250101497384", True)


def test_synthetic_code128_is_never_valid(tmp_path: Path) -> None:
    found = decode(_render("code128", "4250101497384", tmp_path / "c.png"))
    assert [(b.symbology, b.valid) for b in found] == [("Code128", False)]
    assert valid_eans(found) == []


def test_no_barcode(tmp_path: Path) -> None:
    p = tmp_path / "blank.png"
    Image.new("RGB", (200, 100), "white").save(p)
    assert decode(p) == []


def test_rotated_ean(tmp_path: Path) -> None:
    found = decode(_render("ean13", "403690900001", tmp_path / "r.png", rotate=90))
    assert valid_eans(found) == ["4036909000014"]


def test_exif_rotated_original(tmp_path: Path) -> None:
    src = _render("ean13", "403690900001", tmp_path / "r.png", rotate=90)
    jpg = tmp_path / "r.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.open(src).save(jpg, exif=exif)
    assert valid_eans(decode(jpg)) == ["4036909000014"]


def _fixture(prefix: str) -> Path:
    (p,) = FIXTURES.glob(f"{prefix}*")
    return p


@pytest.mark.skipif(not FIXTURES.exists(), reason="fixture photos not present")
def test_fixture_bxr_label() -> None:
    assert valid_eans(decode(_fixture("58dae532"))) == ["4036909000014"]


@pytest.mark.skipif(not FIXTURES.exists(), reason="fixture photos not present")
def test_fixture_kompakt_wrong_ean_never_reported() -> None:
    # Too blurry to decode at this resolution; AC-1 is then covered by ean_digits (Step 6).
    assert valid_eans(decode(_fixture("a236e6b3"))) in ([], ["4250101497384"])


@pytest.mark.skipif(not FIXTURES.exists(), reason="fixture photos not present")
@pytest.mark.parametrize("prefix", ["28213b64", "b2fd7398", "b4fe7e8c", "c761c01c", "d2e01ef6", "e2f803fa"])
def test_fixture_shop_stickers_have_no_valid_ean(prefix: str) -> None:
    assert valid_eans(decode(_fixture(prefix))) == []
