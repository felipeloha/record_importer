from dataclasses import dataclass

import httpx

from crate2discogs.config import Settings
from crate2discogs.discogs import DiscogsClient, DiscogsError, DiscogsNotFound


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings, client: DiscogsClient, http: httpx.Client | None = None) -> list[Check]:
    """Checks run by `init`: Discogs token and username, target folder, extractor readiness."""
    checks: list[Check] = []
    try:
        who = client.identity().get("username")
        ok = who == settings.discogs_username
        detail = f"token belongs to {who!r}" + (
            "" if ok else f", DISCOGS_USERNAME is {settings.discogs_username!r}"
        )
        checks.append(Check("discogs token", ok, detail))
    except DiscogsError as e:
        checks.append(Check("discogs token", False, str(e)))

    try:
        folder = client.folder(settings.discogs_folder_id)
        checks.append(Check("discogs folder", True, f"{settings.discogs_folder_id} = {folder.get('name')!r}"))
    except DiscogsNotFound:
        checks.append(Check("discogs folder", False, f"folder {settings.discogs_folder_id} does not exist"))
    except DiscogsError as e:
        checks.append(Check("discogs folder", False, str(e)))

    if settings.extractor == "ollama":
        http = http or httpx.Client(timeout=5.0)
        try:
            tags = http.get(f"{settings.ollama_url.rstrip('/')}/api/tags").json()
            names = {m.get("name") for m in tags.get("models", [])}
            ok = settings.ollama_model in names
            detail = "model pulled" if ok else f"run `ollama pull {settings.ollama_model}`"
            checks.append(Check(f"ollama {settings.ollama_model}", ok, detail))
        except httpx.HTTPError:
            checks.append(Check("ollama", False, f"cannot reach {settings.ollama_url}: run `ollama serve`"))
    elif settings.api_billing_risk:
        checks.append(Check("claude-code", False, "ANTHROPIC_API_KEY is set: this would bill the API"))
    return checks
