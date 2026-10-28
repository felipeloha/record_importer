from pathlib import Path

import httpx
import pytest
import respx

from crate2discogs.checks import run_checks
from crate2discogs.config import Settings, load_settings
from crate2discogs.discogs import DiscogsClient

API = "https://api.discogs.com"
OLLAMA = "http://localhost:11434"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k in ("EXTRACTOR", "OLLAMA_URL", "OLLAMA_MODEL", "DISCOGS_FOLDER_ID", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DISCOGS_TOKEN", "t")
    monkeypatch.setenv("DISCOGS_USERNAME", "me")
    monkeypatch.setenv("DISCOGS_FOLDER_ID", "5")
    return load_settings(env_file=None)


def _client() -> DiscogsClient:
    return DiscogsClient("t", "me", "ua", sleep=lambda s: None)


@respx.mock
def test_all_ok(settings: Settings, tmp_path: Path) -> None:
    respx.get(f"{API}/oauth/identity").mock(return_value=httpx.Response(200, json={"username": "me"}))
    respx.get(f"{API}/users/me/collection/folders/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "name": "crate2discogs-test"})
    )
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5vl:7b"}]})
    )
    checks = run_checks(settings, _client())
    assert all(c.ok for c in checks), checks


@respx.mock
def test_failures_are_explained(settings: Settings) -> None:
    respx.get(f"{API}/oauth/identity").mock(return_value=httpx.Response(200, json={"username": "other"}))
    respx.get(f"{API}/users/me/collection/folders/5").mock(return_value=httpx.Response(404))
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
    by_name = {c.name: c for c in run_checks(settings, _client())}
    assert not by_name["discogs token"].ok
    assert "other" in by_name["discogs token"].detail
    assert "does not exist" in by_name["discogs folder"].detail
    assert "ollama pull qwen2.5vl:7b" in by_name["ollama qwen2.5vl:7b"].detail


@respx.mock
def test_ollama_down(settings: Settings) -> None:
    respx.get(f"{API}/oauth/identity").mock(return_value=httpx.Response(200, json={"username": "me"}))
    respx.get(f"{API}/users/me/collection/folders/5").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    by_name = {c.name: c for c in run_checks(settings, _client())}
    assert "ollama serve" in by_name["ollama"].detail
