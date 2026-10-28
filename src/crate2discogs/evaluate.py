import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from crate2discogs.barcodes import ean_checksum_valid, valid_eans
from crate2discogs.db import BarcodeRow, Repo
from crate2discogs.extract.base import Extractor, ExtractResult
from crate2discogs.importer import import_file
from crate2discogs.models import Extraction, PhotoType
from crate2discogs.normalize import barcode_key, catno_key, text_key
from crate2discogs.pipeline import run_decode, run_extract, run_match, run_resolve
from crate2discogs.search import Search

FIELDS = ("photo_type", "artist", "title", "label", "catno", "ean", "disc_count")

_KEYS: dict[str, Callable[[Any], Any]] = {
    "photo_type": lambda v: v,
    "artist": text_key,
    "title": text_key,
    "label": text_key,
    "catno": catno_key,
    "ean": barcode_key,
    "disc_count": lambda v: v,
}


class ExpectedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: Path
    photo_type: PhotoType
    artist: str | None = None
    title: str | None = None
    label: str | None = None
    catno: str | None = None
    ean: str | None = None
    disc_count: int | None = None
    expected_release_id: int | None = None
    duplicate_of: Path | None = None


def load_expected(path: Path) -> list[ExpectedEntry]:
    """Load and validate expected.yaml; file paths are resolved relative to it and must exist."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a list of entries")
    entries = [ExpectedEntry.model_validate(item) for item in raw]
    files = set()
    for e in entries:
        e.file = (path.parent / e.file).resolve()
        if not e.file.is_file():
            raise ValueError(f"{path}: file not found: {e.file}")
        files.add(e.file)
    for e in entries:
        if e.duplicate_of is not None:
            e.duplicate_of = (path.parent / e.duplicate_of).resolve()
            if e.duplicate_of not in files:
                raise ValueError(f"{path}: duplicate_of not in the set: {e.duplicate_of}")
    return entries


def predicted_fields(extraction: Extraction | None, barcodes: list[BarcodeRow]) -> dict[str, Any]:
    """Comparable fields. EAN: decoded barcode, else ean_digits if the checksum is valid."""
    e = extraction or Extraction()
    eans = valid_eans(barcodes)
    ean = eans[0] if eans else (e.ean_digits if e.ean_digits and ean_checksum_valid(e.ean_digits) else None)
    return {
        "photo_type": e.photo_type,
        "artist": e.artist,
        "title": e.title,
        "label": e.label,
        "catno": e.catno,
        "ean": ean,
        "disc_count": e.disc_count,
    }


def field_correct(name: str, expected: Any, predicted: Any) -> bool:
    """Null vs null is correct; otherwise compare normalised keys."""
    key = _KEYS[name]
    exp = key(expected) if expected is not None else None
    pred = key(predicted) if predicted is not None else None
    return (exp or None) == (pred or None)


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def field_accuracy(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, FieldScore]:
    scores = {f: FieldScore() for f in FIELDS}
    for expected, predicted in pairs:
        for f in FIELDS:
            scores[f].total += 1
            scores[f].correct += field_correct(f, expected.get(f), predicted.get(f))
    return scores


@dataclass
class PhotoEval:
    file: Path
    expected: dict[str, Any]
    predicted: dict[str, Any]
    error: str | None
    expected_release_id: int | None = None
    outcome: str | None = None
    release_id: int | None = None
    top3: list[int] = field(default_factory=list)

    @property
    def verdict(self) -> str | None:
        """AC-5 per photo: correct_auto | wrong_auto | review_ok | miss | unverified (None: not matched)."""
        if self.outcome is None:
            return None
        if self.expected_release_id is None:
            return "unverified"
        if self.outcome == "auto":
            return "correct_auto" if self.release_id == self.expected_release_id else "wrong_auto"
        if self.outcome == "review" and self.expected_release_id in self.top3:
            return "review_ok"
        return "miss"

    @property
    def wrong_fields(self) -> list[str]:
        return [f for f in FIELDS if not field_correct(f, self.expected.get(f), self.predicted.get(f))]


@dataclass
class EvalReport:
    photos: list[PhotoEval]
    scores: dict[str, FieldScore]
    seconds_per_photo: float | None
    extractor: str = ""
    model: str | None = None
    notes: list[str] = field(default_factory=list)


class _TimedExtractor:
    def __init__(self, inner: Extractor) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.durations: list[float] = []

    def extract(self, image_path: Path) -> ExtractResult:
        start = time.monotonic()
        try:
            return self.inner.extract(image_path)
        finally:
            self.durations.append(time.monotonic() - start)


def run_eval(
    repo: Repo,
    entries: list[ExpectedEntry],
    extractor: Extractor,
    work_dir: Path,
    max_edge: int,
    search: Search | None = None,
    margin: float = 1.0,
    budget: int = 8,
) -> EvalReport:
    """Import, decode and extract the fixture photos (cached in `repo`), then score per field.

    With `search`, also match and resolve, and compare outcomes with expected_release_id.
    """
    ids = {e.file: import_file(repo, e.file, work_dir, max_edge)[0] for e in entries}
    timed = _TimedExtractor(extractor)
    run_decode(repo)
    run_extract(repo, timed, always_extract=True)
    if search is not None:
        run_match(repo, search, budget)
        run_resolve(repo, margin, force=True)

    photos: list[PhotoEval] = []
    for e in entries:
        pid = ids[e.file]
        row = repo.get_extraction(pid)
        extraction = Extraction.model_validate_json(row.parsed_json) if row and row.parsed_json else None
        predicted = predicted_fields(extraction, repo.get_barcodes(pid))
        expected = e.model_dump(include=set(FIELDS))
        res = repo.get_resolution(pid)
        photos.append(
            PhotoEval(
                e.file,
                expected,
                predicted,
                row.error if row else "not extracted",
                e.expected_release_id,
                res.outcome if res else None,
                res.release_id if res else None,
                [c.release_id for c in repo.get_candidates(pid)[:3]],
            )
        )
    by_file = {p.file: p for p in photos}
    notes = []
    for e in entries:
        if e.duplicate_of and by_file[e.file].outcome is not None:
            a, b = by_file[e.file], by_file[e.duplicate_of]
            if a.release_id is None or b.release_id is None:
                state = "unresolved (decide in review)"
            else:
                state = "same release" if a.release_id == b.release_id else "DIFFERENT releases"
            notes.append(f"duplicate {e.file.name} ~ {e.duplicate_of.name}: {state}")
    scores = field_accuracy([(p.expected, p.predicted) for p in photos])
    spp = sum(timed.durations) / len(timed.durations) if timed.durations else None
    return EvalReport(photos, scores, spp, extractor.name, extractor.model, notes)


def format_report(report: EvalReport, total_photos: int = 500) -> str:
    lines = [f"extractor: {report.extractor} {report.model or ''}".rstrip(), "", "field accuracy:"]
    for f, s in report.scores.items():
        lines.append(f"  {f:<11} {s.correct:>3}/{s.total:<3} {s.accuracy:6.0%}")
    if report.seconds_per_photo is not None:
        est = report.seconds_per_photo * total_photos / 3600
        lines.append(
            f"\nextraction time: {report.seconds_per_photo:.1f} s/photo (~{est:.1f} h for {total_photos})"
        )
    else:
        lines.append("\nextraction time: n/a (all extractions cached)")
    verdicts = Counter(p.verdict for p in report.photos if p.verdict)
    if verdicts:
        lines.append("\noutcomes (AC-5: zero wrong_auto, no miss):")
        for v in ("correct_auto", "review_ok", "wrong_auto", "miss", "unverified"):
            lines.append(f"  {v:<13} {verdicts.get(v, 0):>3}")
        for p in report.photos:
            if p.verdict in ("wrong_auto", "miss", "unverified"):
                lines.append(
                    f"    {p.verdict}: {p.file.name} outcome={p.outcome} release={p.release_id} "
                    f"top3={p.top3} expected={p.expected_release_id}"
                )
    lines += [f"  {n}" for n in report.notes]
    failures = [p for p in report.photos if p.wrong_fields or p.error]
    if failures:
        lines.append("\nper-photo differences (expected → predicted):")
        for p in failures:
            lines.append(f"  {p.file.name}" + (f"  error: {p.error}" if p.error else ""))
            for f in p.wrong_fields:
                lines.append(f"    {f}: {p.expected.get(f)!r} → {p.predicted.get(f)!r}")
    return "\n".join(lines)
