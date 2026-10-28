from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from crate2discogs.models import Extraction, postprocess


@dataclass(frozen=True)
class ExtractResult:
    extraction: Extraction
    raw: str


class ExtractionFailed(Exception):
    """This photo could not be extracted; the pipeline marks it extract_failed and continues."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class ExtractorUnavailable(Exception):
    """The extractor backend is unreachable; the stage stops."""


class Extractor(Protocol):
    name: str
    model: str | None

    def extract(self, image_path: Path) -> ExtractResult: ...


def parse(raw: str) -> Extraction:
    """Parse model output into a post-processed Extraction. Raises ValueError on invalid JSON/schema."""
    try:
        return postprocess(Extraction.model_validate_json(raw))
    except ValidationError as e:
        raise ValueError(str(e)) from e
