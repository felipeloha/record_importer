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


def postprocess(e: Extraction) -> Extraction:
    """Move title format hints into structured fields (R-3) and clean EAN digits."""
    title, hints, discs = strip_format_hints(e.title)
    merged = list(dict.fromkeys([*e.format_hints, *hints]))
    return e.model_copy(
        update={
            "title": title or None,
            "format_hints": merged,
            "disc_count": e.disc_count if e.disc_count is not None else discs,
            "ean_digits": barcode_key(e.ean_digits) or None,
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
    return result
