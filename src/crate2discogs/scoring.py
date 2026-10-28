import re
from dataclasses import dataclass, field
from typing import Any, Literal

from rapidfuzz import fuzz

from crate2discogs.barcodes import ean_checksum_valid, valid_eans
from crate2discogs.db import BarcodeRow
from crate2discogs.models import Extraction
from crate2discogs.normalize import barcode_key, catno_key, catno_similar, catno_variants, text_key

# Points per matching field. The score only ranks candidates; the outcome rules are in `resolve`.
WEIGHTS: dict[str, float] = {
    "barcode": 5.0,
    "catno": 4.0,
    "catno_similar": 3.0,
    "artist": 1.5,
    "title": 1.5,
    "label": 1.0,
    "disc_count": 1.0,
    "year": 0.5,
    "country": 0.5,
}
FUZZY_MIN = 85  # rapidfuzz ratio (0-100) counted as a match for label/artist/title

Outcome = Literal["auto", "review", "not_found"]


@dataclass(frozen=True)
class Scored:
    release_id: int
    score: float
    hard_match: bool  # barcode or exact catno matched: required for `auto`
    matched: tuple[str, ...]
    payload: dict[str, Any]
    source_query: dict[str, Any] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        """Identified by barcode or catno (exact or similar): the search can stop."""
        return self.hard_match or "catno_similar" in self.matched


@dataclass(frozen=True)
class Resolution:
    outcome: Outcome
    release_id: int | None
    ranked: list[Scored]


def our_eans(extraction: Extraction | None, barcodes: list[BarcodeRow]) -> list[str]:
    eans = valid_eans(barcodes)
    digits = extraction.ean_digits if extraction else None
    if digits and ean_checksum_valid(digits) and digits not in eans:
        eans.append(digits)
    return eans


def catno_keys(extraction: Extraction | None) -> set[str]:
    """Keys of the printed catno, the model's alternatives and their OCR variants (R-4)."""
    if extraction is None:
        return set()
    raw = [c for c in [extraction.catno, *extraction.catno_candidates] if c]
    keys = {catno_key(c) for c in raw}
    for c in raw:
        keys.update(catno_key(v) for v in catno_variants(c, cap=64))
    return {k for k in keys if k}


def _strip_discogs_suffix(name: str) -> str:
    """Discogs disambiguates names with ' (2)', and marks artists with '*'."""
    return re.sub(r"\s*\(\d+\)$", "", name).rstrip("*")


def candidate_artist_title(c: dict[str, Any]) -> tuple[str, str]:
    """Search results carry 'Artist - Title' in `title`; releases have separate fields."""
    if "artists" in c and c.get("title"):
        artist = " ".join(_strip_discogs_suffix(a.get("name", "")) for a in c["artists"])
        return artist, str(c["title"])
    artist, sep, title = str(c.get("title", "")).partition(" - ")
    return (_strip_discogs_suffix(artist), title) if sep else ("", artist)


def candidate_labels(c: dict[str, Any]) -> list[str]:
    labels = c.get("label") or [lbl.get("name", "") for lbl in c.get("labels", [])]
    return [_strip_discogs_suffix(x) for x in labels if x]


def candidate_catnos(c: dict[str, Any]) -> list[str]:
    if c.get("catno"):
        return [str(c["catno"])]
    return [lbl.get("catno", "") for lbl in c.get("labels", []) if lbl.get("catno")]


def candidate_barcodes(c: dict[str, Any]) -> set[str]:
    raw = c.get("barcode") or [
        i.get("value", "") for i in c.get("identifiers", []) if i.get("type") == "Barcode"
    ]
    return {barcode_key(b) for b in raw if b}


def candidate_disc_count(c: dict[str, Any]) -> int | None:
    if c.get("format_quantity"):
        return int(c["format_quantity"])
    qty = [int(f.get("qty", 0) or 0) for f in c.get("formats", []) if f.get("name") == "Vinyl"]
    return sum(qty) or None


def _fuzzy(a: str | None, options: list[str]) -> bool:
    """Fuzzy match, also with spaces removed: 'BASIC TIONAL VOCABULARY' ≈ 'Basictonalvocabulary'."""
    ka = text_key(a)
    if not ka:
        return False
    for o in options:
        ko = text_key(o)
        if ko and (
            fuzz.token_set_ratio(ka, ko) >= FUZZY_MIN
            or fuzz.ratio(ka.replace(" ", ""), ko.replace(" ", "")) >= FUZZY_MIN
        ):
            return True
    return False


def score(candidate: dict[str, Any], extraction: Extraction | None, barcodes: list[BarcodeRow]) -> Scored:
    e = extraction or Extraction()
    matched: list[str] = []
    if set(our_eans(extraction, barcodes)) & candidate_barcodes(candidate):
        matched.append("barcode")
    theirs = candidate_catnos(candidate)
    if catno_keys(extraction) & {catno_key(c) for c in theirs}:
        matched.append("catno")
    elif e.catno and any(catno_similar(e.catno, c) for c in theirs):
        matched.append("catno_similar")
    artist, title = candidate_artist_title(candidate)
    if _fuzzy(e.artist, [artist]):
        matched.append("artist")
    if _fuzzy(e.title, [title]):
        matched.append("title")
    if _fuzzy(e.label, candidate_labels(candidate)):
        matched.append("label")
    if e.disc_count is not None and e.disc_count == candidate_disc_count(candidate):
        matched.append("disc_count")
    if e.year is not None and str(e.year) == str(candidate.get("year", "")):
        matched.append("year")
    if e.country and text_key(e.country) == text_key(str(candidate.get("country", ""))):
        matched.append("country")
    return Scored(
        release_id=int(candidate["id"]),
        score=sum(WEIGHTS[m] for m in matched),
        hard_match="barcode" in matched or "catno" in matched,
        matched=tuple(matched),
        payload=candidate,
    )


def rank(
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    extraction: Extraction | None,
    barcodes: list[BarcodeRow],
) -> list[Scored]:
    """Score (payload, source_query) pairs, keep the best entry per release, sort best first."""
    best: dict[int, Scored] = {}
    for payload, query in candidates:
        s = score(payload, extraction, barcodes)
        s = Scored(s.release_id, s.score, s.hard_match, s.matched, s.payload, query)
        if s.release_id not in best or s.score > best[s.release_id].score:
            best[s.release_id] = s
    return sorted(best.values(), key=lambda s: (-s.score, s.release_id))


def resolve(ranked: list[Scored], margin: float) -> Resolution:
    """FR-5: `auto` only if the top candidate (a) matches on barcode or catno and (b) leads by `margin`."""
    if not ranked:
        return Resolution("not_found", None, [])
    top = ranked[0]
    leads = len(ranked) == 1 or top.score - ranked[1].score >= margin
    if top.hard_match and leads:
        return Resolution("auto", top.release_id, ranked)
    return Resolution("review", None, ranked)
