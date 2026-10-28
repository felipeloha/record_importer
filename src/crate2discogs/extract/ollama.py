import base64
from pathlib import Path
from typing import Any

import httpx

from crate2discogs.extract.base import ExtractionFailed, ExtractorUnavailable, ExtractResult, parse
from crate2discogs.extract.prompt import PROMPT, STRICT_SUFFIX, USER
from crate2discogs.models import extraction_schema


class OllamaExtractor:
    name = "ollama"

    def __init__(
        self, url: str, model: str, timeout: float = 300.0, client: httpx.Client | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.model: str | None = model
        self.client = client or httpx.Client(timeout=timeout)
        self.schema = extraction_schema()

    def _body(self, image_b64: str, strict: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "stream": False,
            "format": self.schema,
            # A full answer is ~300 tokens; the cap stops a looping model early.
            "options": {"temperature": 0, "num_predict": 768},
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": USER + (STRICT_SUFFIX if strict else ""), "images": [image_b64]},
            ],
        }

    def _chat(self, body: dict[str, Any]) -> str:
        try:
            r = self.client.post(f"{self.url}/api/chat", json=body)
        except httpx.ConnectError as e:
            raise ExtractorUnavailable(
                f"cannot reach Ollama at {self.url}: is `ollama serve` running?"
            ) from e
        except httpx.TimeoutException as e:
            raise ExtractionFailed(f"ollama timeout: {e}") from e
        if r.status_code == 404:
            raise ExtractorUnavailable(
                f"Ollama model {self.model!r} not found: run `ollama pull {self.model}`"
            )
        if r.status_code >= 400:
            raise ExtractionFailed(f"ollama HTTP {r.status_code}: {r.text[:300]}")
        content: str = r.json().get("message", {}).get("content", "")
        return content

    def extract(self, image_path: Path) -> ExtractResult:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode()
        raw = ""
        error = ""
        for strict in (False, True):
            raw = self._chat(self._body(image_b64, strict))
            try:
                return ExtractResult(parse(raw), raw)
            except ValueError as e:
                error = str(e)
        raise ExtractionFailed(f"invalid JSON after retry: {error[:300]}", raw)
