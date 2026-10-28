from pathlib import Path

import pytest
from typer.testing import CliRunner

from crate2discogs.cli import app
from crate2discogs.config import ConfigError, load_settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("DISCOGS_FOLDER_ID", "EXTRACTOR", "OLLAMA_MODEL", "ANTHROPIC_API_KEY", "MAX_EDGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISCOGS_TOKEN", "tok")
    monkeypatch.setenv("DISCOGS_USERNAME", "me")
    return monkeypatch


def test_defaults(env: pytest.MonkeyPatch) -> None:
    s = load_settings(env_file=None)
    assert s.discogs_token == "tok"
    assert s.discogs_username == "me"
    assert s.discogs_folder_id == 1
    assert s.extractor == "ollama"
    assert s.ollama_model == "qwen2.5vl:7b"
    assert s.max_edge == 1600
    assert s.db_path == Path("./crate.db")


def test_values_from_env(env: pytest.MonkeyPatch) -> None:
    env.setenv("DISCOGS_FOLDER_ID", "42")
    env.setenv("EXTRACTOR", "claude-code")
    env.setenv("OLLAMA_MODEL", "qwen3-vl:8b")
    s = load_settings(env_file=None)
    assert (s.discogs_folder_id, s.extractor, s.ollama_model) == (42, "claude-code", "qwen3-vl:8b")


def test_values_from_env_file(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env.delenv("DISCOGS_USERNAME")
    f = tmp_path / ".env"
    f.write_text("DISCOGS_USERNAME=fromfile\n")
    assert load_settings(env_file=f).discogs_username == "fromfile"


def test_missing_token_names_variable(env: pytest.MonkeyPatch) -> None:
    env.delenv("DISCOGS_TOKEN")
    with pytest.raises(ConfigError, match="DISCOGS_TOKEN"):
        load_settings(env_file=None)


def test_folder_zero_rejected(env: pytest.MonkeyPatch) -> None:
    env.setenv("DISCOGS_FOLDER_ID", "0")
    with pytest.raises(ConfigError, match="DISCOGS_FOLDER_ID"):
        load_settings(env_file=None)


def test_api_billing_risk(env: pytest.MonkeyPatch) -> None:
    env.setenv("EXTRACTOR", "claude-code")
    assert load_settings(env_file=None).api_billing_risk is False
    env.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert load_settings(env_file=None).api_billing_risk is True
    env.setenv("EXTRACTOR", "ollama")
    assert load_settings(env_file=None).api_billing_risk is False


def test_help_lists_all_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "import", "identify", "review", "add-to-collection", "status", "export", "eval"):
        assert cmd in result.output
