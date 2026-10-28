import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from crate2discogs.extract.base import ExtractionFailed, ExtractorUnavailable, ExtractResult, parse
from crate2discogs.extract.prompt import PROMPT, STRICT_SUFFIX, USER
from crate2discogs.models import extraction_schema

API_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is set: the claude CLI would bill the API instead of your subscription. "
    "Unset it, or pass --allow-api-billing."
)


class ClaudeCodeExtractor:
    """Runs `claude -p` headless with the image path; uses the logged-in subscription."""

    name = "claude-code"

    def __init__(
        self,
        executable: str = "claude",
        timeout: float = 300.0,
        allow_api_billing: bool = False,
        model: str | None = None,
    ) -> None:
        self.executable = executable
        self.timeout = timeout
        self.allow_api_billing = allow_api_billing
        self.model: str | None = model
        self.schema = json.dumps(extraction_schema())

    def _command(self, image_path: Path, strict: bool) -> list[str]:
        prompt = (
            f"{PROMPT}\n{USER}\nImage file: {image_path}\n"
            "Read the image with the Read tool, then answer with the JSON object only."
            + (STRICT_SUFFIX if strict else "")
        )
        cmd = [
            self.executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--json-schema",
            self.schema,
            "--add-dir",
            str(image_path.parent),
            "--allowedTools",
            "Read",
        ]
        return [*cmd, "--model", self.model] if self.model else cmd

    def _run(self, image_path: Path, strict: bool) -> str:
        try:
            proc = subprocess.run(
                self._command(image_path, strict),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=image_path.parent,
            )
        except FileNotFoundError as e:
            raise ExtractorUnavailable(f"`{self.executable}` not found: is Claude Code installed?") from e
        except subprocess.TimeoutExpired as e:
            raise ExtractionFailed(f"claude timeout after {self.timeout:.0f} s") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ExtractionFailed(f"claude exited {proc.returncode}: {detail}", proc.stdout or None)
        return proc.stdout

    @staticmethod
    def _payload(stdout: str) -> str:
        """The extraction JSON from `--output-format json` output: structured_output, else the result text."""
        envelope: Any = json.loads(stdout)
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                raise ValueError(f"claude reported an error: {str(envelope.get('result'))[:300]}")
            if isinstance(envelope.get("structured_output"), dict):
                return json.dumps(envelope["structured_output"])
            if isinstance(envelope.get("result"), str):
                return re.sub(r"^```(?:json)?\s*|\s*```$", "", envelope["result"].strip())
        raise ValueError("unexpected claude output")

    def extract(self, image_path: Path) -> ExtractResult:
        if os.environ.get("ANTHROPIC_API_KEY") and not self.allow_api_billing:
            raise ExtractorUnavailable(API_KEY_MESSAGE)
        stdout, error = "", ""
        for strict in (False, True):
            stdout = self._run(image_path, strict)
            try:
                return ExtractResult(parse(self._payload(stdout)), stdout)
            except (ValueError, json.JSONDecodeError) as e:
                error = str(e)
        raise ExtractionFailed(f"invalid output after retry: {error[:300]}", stdout)
