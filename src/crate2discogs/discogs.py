import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from crate2discogs.db import Repo
from crate2discogs.ratelimit import TokenBucket

log = logging.getLogger(__name__)

BASE_URL = "https://api.discogs.com"


class DiscogsError(Exception):
    pass


class DiscogsNotFound(DiscogsError):
    pass


class DiscogsClient:
    """Discogs API client: auth, rate limit, retries, and a SQLite cache for search and release GETs.

    Collection and identity calls are never cached, so re-runs always see the live collection.
    """

    def __init__(
        self,
        token: str,
        username: str,
        user_agent: str,
        repo: Repo | None = None,
        cache_ttl: float = 30 * 86400,
        bucket: TokenBucket | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
        attempts: int = 3,
        exhausted_pause: float = 10.0,
        rate_limit_waits: tuple[float, ...] = (5, 10, 20, 40, 60, 60),
    ) -> None:
        self.username = username
        self.repo = repo
        self.cache_ttl = cache_ttl
        self.clock = clock
        self.sleep = sleep
        # Discogs counts 60 requests per moving minute. Pace evenly (no burst) slightly below that.
        self.bucket = bucket or TokenBucket(55, 60.0, capacity=1, sleep=sleep)
        self.base_url = base_url.rstrip("/")
        self.attempts = attempts
        self.exhausted_pause = exhausted_pause
        self.rate_limit_waits = rate_limit_waits
        self._pause_next = 0.0
        self.http = client or httpx.Client(timeout=30.0)
        self.headers = {"Authorization": f"Discogs token={token}", "User-Agent": user_agent}

    # public API

    def identity(self) -> dict[str, Any]:
        return self._get("/oauth/identity", cache=False)

    def search(self, **params: Any) -> dict[str, Any]:
        return self._get("/database/search", params, cache=True)

    def release(self, release_id: int) -> dict[str, Any]:
        return self._get(f"/releases/{release_id}", cache=True)

    def folder(self, folder_id: int) -> dict[str, Any]:
        return self._get(f"/users/{self.username}/collection/folders/{folder_id}", cache=False)

    def collection_instances(self, release_id: int) -> list[dict[str, Any]]:
        """Instances of a release in the user's collection (all folders). Never cached."""
        data = self._get(f"/users/{self.username}/collection/releases/{release_id}", cache=False)
        releases: list[dict[str, Any]] = data.get("releases", [])
        return releases

    def add_release(self, folder_id: int, release_id: int) -> int:
        """Add one instance to a folder. Returns the new instance_id."""
        path = f"/users/{self.username}/collection/folders/{folder_id}/releases/{release_id}"
        data = self._request("POST", path, None)
        return int(data["instance_id"])

    # internals

    def _get(self, path: str, params: dict[str, Any] | None = None, *, cache: bool) -> dict[str, Any]:
        key = f"GET {path}?{urlencode(sorted((params or {}).items()))}"
        if cache and self.repo is not None:
            entry = self.repo.cache_get(key)
            if entry is not None and self.clock() - entry.fetched_at < self.cache_ttl:
                cached: dict[str, Any] = json.loads(entry.body)
                return cached
        data = self._request("GET", path, params)
        if cache and self.repo is not None:
            self.repo.cache_put(key, 200, json.dumps(data), fetched_at=self.clock())
        return data

    def _request(self, method: str, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Send with pacing. 429: wait (Retry-After, else 5→60 s) and retry; 5xx/network: 3 attempts."""
        url = self.base_url + path
        last = ""
        errors = 0  # 5xx and network errors
        limited = 0  # 429 responses
        sent = 0
        while True:
            if self._pause_next:
                self.sleep(self._pause_next)
                self._pause_next = 0.0
            self.bucket.acquire()
            sent += 1
            try:
                r = self.http.request(method, url, params=params, headers=self.headers)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
                errors += 1
                if errors >= self.attempts:
                    break
                self.sleep(2 ** (errors - 1))
                continue
            remaining = r.headers.get("X-Discogs-Ratelimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
                self._pause_next = self.exhausted_pause
            if r.status_code == 429:
                last = "HTTP 429 (rate limited)"
                if limited >= len(self.rate_limit_waits):
                    break
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else self.rate_limit_waits[limited]
                limited += 1
                log.info("discogs rate limited path=%s wait=%ss", path, wait)
                self.sleep(wait)
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                errors += 1
                if errors >= self.attempts:
                    break
                log.info("discogs retry method=%s path=%s status=%s", method, path, r.status_code)
                self.sleep(2 ** (errors - 1))
                continue
            if r.status_code == 404:
                raise DiscogsNotFound(f"{method} {path}: not found")
            if r.status_code >= 400:
                raise DiscogsError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
            body: dict[str, Any] = r.json() if r.content else {}
            return body
        raise DiscogsError(f"{method} {path}: gave up after {sent} attempts ({last})")
