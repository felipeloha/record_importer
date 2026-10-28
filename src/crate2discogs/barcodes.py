from pathlib import Path

import zxingcpp

from crate2discogs.db import BarcodeRow
from crate2discogs.importer import open_oriented

# R-1: only these symbologies can identify a release.
EAN_SYMBOLOGIES = frozenset({"EAN13", "EAN8", "UPCA", "UPCE"})


def ean_checksum_valid(digits: str) -> bool:
    """GTIN check digit for EAN-8, UPC-A (12) and EAN-13."""
    if len(digits) not in (8, 12, 13) or not digits.isdigit():
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def upce_to_upca(upce: str) -> str:
    """Expand an 8-digit UPC-E (number system + 6 digits + check) to UPC-A."""
    ns, d, check = upce[0], upce[1:7], upce[7]
    last = d[5]
    if last in "012":
        body = d[0:2] + last + "0000" + d[2:5]
    elif last == "3":
        body = d[0:3] + "00000" + d[3:5]
    elif last == "4":
        body = d[0:4] + "00000" + d[4]
    else:
        body = d[0:5] + "0000" + last
    return ns + body + check


def _to_row(symbology: str, text: str) -> BarcodeRow:
    if symbology not in EAN_SYMBOLOGIES:
        return BarcodeRow(symbology, text, False)
    value = upce_to_upca(text) if symbology == "UPCE" and len(text) == 8 else text
    return BarcodeRow(symbology, value, ean_checksum_valid(value))


def decode(path: Path) -> list[BarcodeRow]:
    """Decode linear barcodes on the full-resolution, EXIF-oriented image.

    Non-EAN symbologies (e.g. shop stickers' Code93/Code128) are returned with valid=False so they are
    logged but never used as identifiers.
    """
    img = open_oriented(path)
    results = zxingcpp.read_barcodes(
        img, formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.AllLinear), try_rotate=True
    )
    rows: list[BarcodeRow] = []
    seen: set[tuple[str, str]] = set()
    for r in results:
        key = (r.format.name, r.text)
        if key not in seen:
            seen.add(key)
            rows.append(_to_row(*key))
    return rows


def valid_eans(barcodes: list[BarcodeRow]) -> list[str]:
    return [b.value for b in barcodes if b.valid and b.symbology in EAN_SYMBOLOGIES]
