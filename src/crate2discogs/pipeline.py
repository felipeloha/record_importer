import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from crate2discogs.barcodes import decode, valid_eans
from crate2discogs.db import BarcodeRow, CandidateRow, ExtractionRow, Photo, Repo, ResolutionRow, Stage
from crate2discogs.extract.base import ExtractionFailed, Extractor
from crate2discogs.models import Extraction, postprocess
from crate2discogs.scoring import Scored, rank, resolve
from crate2discogs.search import Search, run_search

log = logging.getLogger(__name__)

Decoder = Callable[[Path], list[BarcodeRow]]
Tick = Callable[[], None]
StepName = Literal["decode", "extract", "match", "resolve"]
STEPS: tuple[StepName, ...] = ("decode", "extract", "match", "resolve")


def _noop() -> None:
    pass


def load_extraction(repo: Repo, photo_id: int) -> Extraction | None:
    """Stored extraction, re-cleaned so post-processing fixes apply without re-running the model."""
    row = repo.get_extraction(photo_id)
    return postprocess(Extraction.model_validate_json(row.parsed_json)) if row and row.parsed_json else None


def candidate_rows(ranked: list[Scored]) -> list[CandidateRow]:
    return [
        CandidateRow(
            s.release_id, i, s.score, json.dumps(s.source_query, sort_keys=True), json.dumps(s.payload)
        )
        for i, s in enumerate(ranked)
    ]


def _select(repo: Repo, stages: tuple[Stage, ...], limit: int | None) -> list[Photo]:
    photos: list[Photo] = []
    for stage in stages:
        remaining = None if limit is None else limit - len(photos)
        if remaining is not None and remaining <= 0:
            break
        photos += repo.photos_at_stage(stage, remaining)
    return photos


def input_stages(step: StepName, force: bool = False) -> tuple[Stage, ...]:
    """Photo stages a pipeline step picks up."""
    inputs: dict[StepName, tuple[Stage, ...]] = {
        "decode": ("imported",),
        "extract": ("decoded",),
        "match": ("extracted", "extract_failed", "matched", "resolved")
        if force
        else ("extracted", "extract_failed"),
        "resolve": ("matched", "resolved") if force else ("matched",),
    }
    return inputs[step]


def pending(repo: Repo, step: StepName, force: bool = False, limit: int | None = None) -> int:
    return len(_select(repo, input_stages(step, force), limit))


def run_decode(repo: Repo, limit: int | None = None, decoder: Decoder = decode, tick: Tick = _noop) -> int:
    """Decode barcodes for photos at `imported`. Returns the number of photos processed."""
    done = 0
    for photo in _select(repo, input_stages("decode"), limit):
        try:
            barcodes = decoder(photo.path)
        except Exception as e:
            log.warning("decode photo=%s error=%s", photo.id, e)
            repo.set_error(photo.id, f"decode: {e}")
            tick()
            continue
        repo.replace_barcodes(photo.id, barcodes)
        repo.set_error(photo.id, None)
        repo.set_stage(photo.id, "decoded")
        log.info("decode photo=%s barcodes=%s", photo.id, [(b.symbology, b.value, b.valid) for b in barcodes])
        done += 1
        tick()
    return done


def run_extract(
    repo: Repo,
    extractor: Extractor,
    always_extract: bool = True,
    limit: int | None = None,
    tick: Tick = _noop,
) -> int:
    """Run the vision extractor on photos at `decoded`.

    Every photo is extracted, so a barcode unknown to Discogs still has text to fall back on.
    `always_extract=False` skips photos with a valid EAN (faster, no fallback).
    ExtractionFailed → stage extract_failed; ExtractorUnavailable and other errors stop the stage.
    """
    done = 0
    for photo in _select(repo, input_stages("extract"), limit):
        if not always_extract and valid_eans(repo.get_barcodes(photo.id)):
            repo.set_stage(photo.id, "extracted")
            log.info("extract photo=%s skipped=valid_ean", photo.id)
        else:
            try:
                result = extractor.extract(photo.work_path)
            except ExtractionFailed as e:
                repo.save_extraction(
                    photo.id, ExtractionRow(extractor.name, extractor.model, e.raw, None, str(e))
                )
                repo.set_error(photo.id, f"extract: {e}")
                repo.set_stage(photo.id, "extract_failed")
                log.warning("extract photo=%s failed=%s", photo.id, e)
            else:
                parsed = result.extraction.model_dump_json()
                repo.save_extraction(
                    photo.id, ExtractionRow(extractor.name, extractor.model, result.raw, parsed, None)
                )
                repo.set_error(photo.id, None)
                repo.set_stage(photo.id, "extracted")
                log.info(
                    "extract photo=%s catno=%r label=%r",
                    photo.id,
                    result.extraction.catno,
                    result.extraction.label,
                )
        done += 1
        tick()
    return done


def run_match(
    repo: Repo,
    search: Search,
    budget: int = 12,
    variant_cap: int = 6,
    limit: int | None = None,
    tick: Tick = _noop,
    force: bool = False,
) -> int:
    """Search Discogs for photos at `extracted` or `extract_failed` and store the candidates.

    `force` also re-searches matched/resolved photos (not added ones); user choices are kept by resolve.
    Discogs errors (after retries) stop the stage; a re-run continues where it stopped.
    """
    photos = _select(repo, input_stages("match", force), limit)
    for photo in photos:
        extraction = load_extraction(repo, photo.id)
        barcodes = repo.get_barcodes(photo.id)
        result = run_search(search, extraction, barcodes, budget, variant_cap)
        repo.replace_candidates(photo.id, candidate_rows(rank(result.candidates, extraction, barcodes)))
        repo.set_stage(photo.id, "matched")
        log.info("match photo=%s calls=%s candidates=%s", photo.id, result.calls, len(result.candidates))
        tick()
    return len(photos)


def resolve_photo(repo: Repo, photo: Photo, margin: float) -> bool:
    """Score one photo's stored candidates and save the automatic outcome.

    Returns False when a user resolution exists and was kept (conflict rule).
    """
    stored = [(json.loads(c.payload_json), json.loads(c.source_query)) for c in repo.get_candidates(photo.id)]
    ranked = rank(stored, load_extraction(repo, photo.id), repo.get_barcodes(photo.id))
    result = resolve(ranked, margin)
    repo.replace_candidates(photo.id, candidate_rows(ranked))
    written = repo.save_resolution(photo.id, ResolutionRow(result.outcome, result.release_id, "auto"))
    if photo.stage == "matched":
        repo.set_stage(photo.id, "resolved")
    log.info(
        "resolve photo=%s outcome=%s release=%s kept_user_choice=%s",
        photo.id,
        result.outcome,
        result.release_id,
        not written,
    )
    return written


def run_resolve(
    repo: Repo, margin: float, force: bool = False, limit: int | None = None, tick: Tick = _noop
) -> int:
    """Decide the outcome for photos at `matched`.

    `force` also re-scores `resolved` photos (no Discogs calls). User resolutions are never replaced.
    """
    photos = _select(repo, input_stages("resolve", force), limit)
    for photo in photos:
        resolve_photo(repo, photo, margin)
        tick()
    return len(photos)


def identify(
    repo: Repo,
    *,
    extractor: Callable[[], Extractor],
    search: Callable[[], Search],
    margin: float,
    budget: int = 12,
    variant_cap: int = 6,
    step: StepName | None = None,
    limit: int | None = None,
    always_extract: bool = True,
    force: bool = False,
    decoder: Decoder = decode,
    progress: Callable[[StepName, int], Tick] | None = None,
) -> dict[StepName, int]:
    """Run the non-interactive steps in order (or one `step`). Resumable: each step picks photos by stage.

    `extractor` and `search` are factories so a step that has no work never needs Ollama or Discogs.
    `progress(step, total)` returns a tick callback, e.g. for a progress bar.
    """
    done: dict[StepName, int] = {}
    for name in (step,) if step else STEPS:
        total = pending(repo, name, force, limit)
        if total == 0:
            done[name] = 0
            continue
        tick = progress(name, total) if progress else _noop
        if name == "decode":
            done[name] = run_decode(repo, limit, decoder, tick)
        elif name == "extract":
            done[name] = run_extract(repo, extractor(), always_extract, limit, tick)
        elif name == "match":
            done[name] = run_match(repo, search(), budget, variant_cap, limit, tick, force)
        else:
            done[name] = run_resolve(repo, margin, force, limit, tick)
    return done
