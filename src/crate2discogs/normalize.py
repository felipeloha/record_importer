import itertools
import re
import unicodedata

_OCR_SWAPS = {"O": "0", "0": "O", "I": "1", "1": "I", "S": "5", "5": "S", "B": "8", "8": "B"}

# Parenthesised groups that are format hints, not part of the title (R-3).
_HINT_PATTERNS = [
    re.compile(r"^(\d*)\s*X?\s*LP$"),
    re.compile(r'^(\d+)\s*X\s*(?:12|10|7)"?$'),
    re.compile(r'^(?:12|10|7)"$'),
    re.compile(r"^VINYL(?: ONLY)?$"),
    re.compile(r"^(?:EP|LTD\.?|LIMITED(?: EDITION)?|REPRESS|180\s*G(?:RAM)?|COLOU?RED(?: VINYL)?)$"),
]
_TRAILING_GROUP = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]\s*$")
_UPPER_HINT = re.compile(r'^[\dX\s]*(?:LP|EP|G|"|12|10|7)$')


def catno_key(s: str | None) -> str:
    """Comparison key: uppercase without spaces, dots, hyphens, slashes. Never sent as a query."""
    return re.sub(r"[\s.\-/_]", "", s or "").upper()


def catno_variants(s: str | None, cap: int = 6) -> list[str]:
    """OCR-confusion variants (O↔0, I↔1, S↔5, B↔8) at positions next to a digit.

    Keeps the original spacing and punctuation. Ordered by number of swaps, deduped, original excluded.
    """
    if not s:
        return []
    chars = list(s)
    positions = [
        i
        for i, c in enumerate(chars)
        if c.upper() in _OCR_SWAPS and any(0 <= j < len(chars) and chars[j].isdigit() for j in (i - 1, i + 1))
    ]
    out: list[str] = []
    seen = {s}
    for n in range(1, len(positions) + 1):
        for combo in itertools.combinations(positions, n):
            v = chars.copy()
            for i in combo:
                v[i] = _OCR_SWAPS[v[i].upper()]
            variant = "".join(v)
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
                if len(out) >= cap:
                    return out
    return out


def _canonical_hint(h: str) -> str:
    return h if _UPPER_HINT.match(h) else h.title()


def strip_format_hints(title: str | None) -> tuple[str | None, list[str], int | None]:
    """Remove trailing format hints like "(2LP)" from a title. Returns (title, hints, disc_count)."""
    if title is None:
        return None, [], None
    clean = title
    hints: list[str] = []
    discs: int | None = None
    while m := _TRAILING_GROUP.search(clean):
        content = re.sub(r"\s+", " ", m.group(1).strip().upper())
        matched = next((p.match(content) for p in _HINT_PATTERNS if p.match(content)), None)
        if matched is None:
            break
        hints.insert(0, _canonical_hint(content))
        if matched.groups() and matched.group(1):
            discs = int(matched.group(1))
        clean = clean[: m.start()]
    return clean.strip(), hints, discs


def text_key(s: str | None) -> str:
    """Comparison key for names and titles: accent-, case- and punctuation-insensitive, & = and."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]|_", " ", s)
    return " ".join(s.split())


def barcode_key(s: str | None) -> str:
    """Digits only; Discogs stores barcodes with spaces and dashes."""
    return re.sub(r"\D", "", s or "")
