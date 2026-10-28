import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from crate2discogs.normalize import barcode_key, strip_format_hints

PhotoType = Literal["ean", "shop_sticker", "center_label", "back_cover", "other"]


class Track(BaseModel):
    position: str | None = None
    title: str | None = None


class Extraction(BaseModel):
    """What the vision model read from one photo (FR-3). Every field may be null."""

    photo_type: PhotoType | None = None
    artist: str | None = None
    title: str | None = None
    label: str | None = None
    catno: str | None = Field(default=None, description="Catalog number exactly as printed")
    catno_candidates: list[str] = Field(default_factory=list)
    ean_digits: str | None = Field(default=None, description="Digits printed under an EAN/UPC barcode")
    format_hints: list[str] = Field(default_factory=list)
    disc_count: int | None = None
    year: int | None = None
    country: str | None = None
    tracklist: list[Track] = Field(default_factory=list)
    shop_code: str | None = Field(default=None, description="Record-shop code; captured only to be ignored")
    confidence: float | None = Field(default=None, ge=0, le=1)


def _clean_hint(h: str) -> str:
    _, hints, _ = strip_format_hints(f"x ({h.strip().strip('()[]')})")
    return hints[0] if hints else h.strip().strip("()[]").strip()


def _clean_catno(catno: str | None, label: str | None) -> str | None:
    """Drop a leading label name the model copied into the catno: 'M-PLANT M.PM21' → 'M.PM21'."""
    if not catno:
        return None
    first, _, rest = catno.strip().partition(" ")
    label_words = (label or "").upper().split()
    if rest and label_words and first.upper() == label_words[0]:
        return rest.strip()
    return catno.strip()


def postprocess(e: Extraction) -> Extraction:
    """Move title format hints into structured fields (R-3); clean EAN digits, hints and country."""
    title, hints, discs = strip_format_hints(e.title)
    merged = list(dict.fromkeys(h for h in (_clean_hint(x) for x in [*e.format_hints, *hints]) if h))
    if discs is None:
        discs = next((d for _, _, d in (strip_format_hints(f"x ({h})") for h in merged) if d), None)
    catno, ean_digits = _clean_catno(e.catno, e.label), barcode_key(e.ean_digits) or None
    if catno and catno.isdigit() and len(catno) in (8, 12, 13):
        ean_digits, catno = ean_digits or catno, None  # the model put the EAN digits in catno
    country = re.sub(r"^\s*(?:made|printed|manufactured)\s+in\s+", "", e.country or "", flags=re.IGNORECASE)
    return e.model_copy(
        update={
            "title": title or None,
            "format_hints": merged,
            "disc_count": e.disc_count if e.disc_count is not None else discs,
            "ean_digits": ean_digits,
            "catno": catno,
            "country": country.strip() or None,
        }
    )


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].split("/")[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def extraction_schema() -> dict[str, Any]:
    """JSON schema for structured output, with $refs inlined for model runtimes that don't resolve them."""
    schema = Extraction.model_json_schema()
    result: dict[str, Any] = _inline_refs(schema, schema.get("$defs", {}))
    _require_all(result)
    return result


def _require_all(node: Any) -> None:
    """Mark every property required (values stay nullable) so the model answers each field."""
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"])
        for v in node.values():
            _require_all(v)
    elif isinstance(node, list):
        for v in node:
            _require_all(v)
