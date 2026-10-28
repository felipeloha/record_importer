from crate2discogs.config import Settings
from crate2discogs.extract.base import Extractor


def make_extractor(settings: Settings) -> Extractor:
    if settings.extractor == "ollama":
        from crate2discogs.extract.ollama import OllamaExtractor

        return OllamaExtractor(settings.ollama_url, settings.ollama_model)
    raise NotImplementedError(f"extractor {settings.extractor!r} is not implemented yet")
