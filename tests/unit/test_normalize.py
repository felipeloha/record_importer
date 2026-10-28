import pytest

from crate2discogs.normalize import (
    barcode_key,
    catno_key,
    catno_similar,
    catno_variants,
    strip_format_hints,
    text_key,
)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("BXRCG 003-12", "BXRCG00312"),
        ("M.PM21", "MPM21"),
        ("DKMNTL 031", "DKMNTL031"),
        ("Perlon100", "PERLON100"),
        ("TRESOR 100/73", "TRESOR10073"),
        ("", ""),
    ],
)
def test_catno_key(raw: str, key: str) -> None:
    assert catno_key(raw) == key


def test_catno_variants_ocr_swap() -> None:
    assert "LOG016RE" in catno_variants("LOGO16RE")
    assert "LOGO16RE" in catno_variants("LOG016RE")


def test_catno_variants_only_next_to_digit() -> None:
    variants = catno_variants("PERLON100")
    assert all(v.upper().startswith("PERLON") for v in variants)
    assert "PERL0N100" not in variants


def test_catno_variants_cap_and_order() -> None:
    variants = catno_variants("SO1OB5O", cap=3)
    assert len(variants) == 3
    single = catno_variants("SO1OB5O", cap=100)
    # Ordered by number of swaps: every one-swap variant comes before any two-swap variant.
    diffs = [sum(a != b for a, b in zip(v, "SO1OB5O", strict=True)) for v in single]
    assert diffs == sorted(diffs)
    assert len(set(single)) == len(single)
    assert "SO1OB5O" not in single


def test_catno_variants_keep_punctuation() -> None:
    variants = catno_variants("BXRCG 003-12")
    assert variants
    assert all(" " in v and "-" in v for v in variants)


def test_catno_variants_none() -> None:
    assert catno_variants("ABC") == []
    assert catno_variants("") == []


@pytest.mark.parametrize(
    ("title", "clean", "hints", "discs"),
    [
        ("BASICTONALVOCABULARY (2XLP)", "BASICTONALVOCABULARY", ["2XLP"], 2),
        ("IN WOOD, THAT (VINYL ONLY)", "IN WOOD, THAT", ["Vinyl Only"], None),
        ("PARADYGM SHIFT (2LP)", "PARADYGM SHIFT", ["2LP"], 2),
        ("Album (3LP) (180g)", "Album", ["3LP", "180G"], 3),
        ('Thing (2x12")', "Thing", ['2X12"'], 2),
        ("Track (Remix)", "Track (Remix)", [], None),
        ("Plain", "Plain", [], None),
    ],
)
def test_strip_format_hints(title: str, clean: str, hints: list[str], discs: int | None) -> None:
    assert strip_format_hints(title) == (clean, hints, discs)


def test_text_key() -> None:
    assert text_key("Mario Piu' & Mauro Picotto") == text_key("MARIO PIÙ and MAURO PICOTTO")
    assert text_key("  Paradygm   Shift - Volume 1 ") == "paradygm shift volume 1"
    assert text_key(None) == ""


def test_barcode_key() -> None:
    assert barcode_key("4 250101 497384") == "4250101497384"
    assert barcode_key("4-036909-000014 (EAN)") == "4036909000014"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Perlon100", "PERL100", True),
        ("TRESOR10073LP", "Tresor. 10073", True),
        ("LOG016RE", "log 16", True),
        ("DKMNTL 031", "DKMNTL031", True),
        ("M.PM21", "MPM 213", False),
        ("DKMNTL 031", "DKMNTL 050", False),
        ("LOG016RE", "ABC16", False),
        ("", "X1", False),
    ],
)
def test_catno_similar(a: str, b: str, same: bool) -> None:
    assert catno_similar(a, b) is same
