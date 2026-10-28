from crate2discogs.config import Settings
from crate2discogs.extract.base import Extractor


def make_extractor(settings: Settings, allow_api_billing: bool = False) -> Extractor:
    if settings.extractor == "claude-code":
        from crate2discogs.extract.claude_code import ClaudeCodeExtractor

        return ClaudeCodeExtractor(allow_api_billing=allow_api_billing)
    from crate2discogs.extract.ollama import OllamaExtractor

    return OllamaExtractor(settings.ollama_url, settings.ollama_model)
