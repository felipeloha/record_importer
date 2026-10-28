import logging
from collections.abc import Callable
from pathlib import Path

from crate2discogs.barcodes import decode, valid_eans
from crate2discogs.db import BarcodeRow, ExtractionRow, Repo
from crate2discogs.extract.base import ExtractionFailed, Extractor

log = logging.getLogger(__name__)

Decoder = Callable[[Path], list[BarcodeRow]]


def run_decode(repo: Repo, limit: int | None = None, decoder: Decoder = decode) -> int:
    """Decode barcodes for photos at `imported`. Returns the number of photos processed."""
    done = 0
    for photo in repo.photos_at_stage("imported", limit):
        try:
            barcodes = decoder(photo.path)
        except Exception as e:
            log.warning("decode photo=%s error=%s", photo.id, e)
            repo.set_error(photo.id, f"decode: {e}")
            continue
        repo.replace_barcodes(photo.id, barcodes)
        repo.set_error(photo.id, None)
        repo.set_stage(photo.id, "decoded")
        log.info("decode photo=%s barcodes=%s", photo.id, [(b.symbology, b.value, b.valid) for b in barcodes])
        done += 1
    return done


def run_extract(
    repo: Repo, extractor: Extractor, always_extract: bool = False, limit: int | None = None
) -> int:
    """Run the vision extractor on photos at `decoded`.

    Photos with a valid EAN are skipped (no extraction row) unless `always_extract`.
    ExtractionFailed → stage extract_failed; ExtractorUnavailable and other errors stop the stage.
    """
    done = 0
    for photo in repo.photos_at_stage("decoded", limit):
        if not always_extract and valid_eans(repo.get_barcodes(photo.id)):
            repo.set_stage(photo.id, "extracted")
            log.info("extract photo=%s skipped=valid_ean", photo.id)
            done += 1
            continue
        try:
            result = extractor.extract(photo.work_path)
        except ExtractionFailed as e:
            repo.save_extraction(
                photo.id, ExtractionRow(extractor.name, extractor.model, e.raw, None, str(e))
            )
            repo.set_error(photo.id, f"extract: {e}")
            repo.set_stage(photo.id, "extract_failed")
            log.warning("extract photo=%s failed=%s", photo.id, e)
            done += 1
            continue
        parsed = result.extraction.model_dump_json()
        repo.save_extraction(
            photo.id, ExtractionRow(extractor.name, extractor.model, result.raw, parsed, None)
        )
        repo.set_error(photo.id, None)
        repo.set_stage(photo.id, "extracted")
        log.info(
            "extract photo=%s catno=%r label=%r", photo.id, result.extraction.catno, result.extraction.label
        )
        done += 1
    return done
