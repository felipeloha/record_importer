import logging
from collections.abc import Callable
from pathlib import Path

from crate2discogs.barcodes import decode
from crate2discogs.db import BarcodeRow, Repo

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
