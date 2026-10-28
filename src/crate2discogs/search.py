import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crate2discogs.db import BarcodeRow
from crate2discogs.models import Extraction
from crate2discogs.normalize import catno_variants
from crate2discogs.scoring import our_eans, rank

BASE_PARAMS = {"type": "release", "format": "Vinyl", "per_page": 25}

# R-2: record-shop codes like "c3f - ic+" or "bw7 - j6*" are never identifiers.
_SHOP_CODE = re.compile(r"^[a-z0-9]{3}\s*-\s*[a-z0-9]{2}[+*]?$", re.IGNORECASE)

Search = Callable[..., dict[str, Any]]


def _clean(value: str | None, shop_code: str | None) -> str | None:
    if not value or not value.strip():
        return None
    v = value.strip()
    if _SHOP_CODE.match(v) or (shop_code and v.casefold() == shop_code.strip().casefold()):
        return None
    return v


def build_queries(
    extraction: Extraction | None, barcodes: list[BarcodeRow], variant_cap: int = 6
) -> list[dict[str, Any]]:
    """FR-4 query order. Catno queries use the printed form or its OCR variants, never catno_key."""
    e = extraction or Extraction()
    artist, title = _clean(e.artist, e.shop_code), _clean(e.title, e.shop_code)
    label, catno = _clean(e.label, e.shop_code), _clean(e.catno, e.shop_code)

    queries: list[dict[str, Any]] = [{"barcode": ean} for ean in our_eans(extraction, barcodes)]
    if catno:
        queries.append({"catno": catno, "label": label} if label else {"catno": catno})
    # Shop stickers often print their own catno spelling, so artist/label queries come before OCR guesses.
    if artist and title:
        queries.append({"artist": artist, "release_title": title})
    if artist and label:
        queries.append({"artist": artist, "label": label})
    if artist or title:
        queries.append({"q": " ".join(x for x in (artist, title) if x)})
    if catno:
        if label:
            queries.append({"catno": catno})
        alternatives = [c for c in (_clean(x, e.shop_code) for x in e.catno_candidates) if c]
        for v in [*alternatives, *catno_variants(catno, cap=variant_cap)]:
            queries.append({"catno": v})

    out: list[dict[str, Any]] = []
    for q in queries:
        full = BASE_PARAMS | q
        if full not in out:
            out.append(full)
    return out


@dataclass(frozen=True)
class SearchResult:
    candidates: list[tuple[dict[str, Any], dict[str, Any]]]  # (payload, source_query)
    calls: int


def run_search(
    search: Search,
    extraction: Extraction | None,
    barcodes: list[BarcodeRow],
    budget: int = 8,
    variant_cap: int = 6,
) -> SearchResult:
    """Run queries in order until a candidate matches on barcode or catno (exact or similar), or the budget
    is used.

    Further queries could only add competitors; `resolve` then decides auto or review.
    """
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    calls = 0
    for query in build_queries(extraction, barcodes, variant_cap)[:budget]:
        results = search(**query).get("results", [])
        calls += 1
        candidates.extend((r, query) for r in results)
        if any(s.found for s in rank(candidates, extraction, barcodes)):
            break
    return SearchResult(candidates, calls)
