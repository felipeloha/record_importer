import os
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    discogs_token: str
    discogs_username: str
    discogs_folder_id: int = Field(default=1, gt=0)
    extractor: Literal["ollama", "claude-code"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"
    max_edge: int = Field(default=1600, gt=0)
    auto_margin: float = Field(default=1.0, ge=0)
    query_budget: int = Field(default=12, gt=0)
    catno_variant_cap: int = Field(default=2, ge=0)
    cache_ttl_days: int = Field(default=30, ge=0)
    db_path: Path = Path("./crate.db")
    user_agent: str = "crate2discogs/0.1"

    @property
    def api_billing_risk(self) -> bool:
        """True when the claude-code extractor would bill the API instead of the subscription."""
        return self.extractor == "claude-code" and bool(os.environ.get("ANTHROPIC_API_KEY"))


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as e:
        problems = []
        for err in e.errors():
            name = str(err["loc"][0]).upper() if err["loc"] else "?"
            problems.append(f"{name}: {err['msg']}")
        raise ConfigError("Invalid configuration:\n  " + "\n  ".join(problems)) from None
