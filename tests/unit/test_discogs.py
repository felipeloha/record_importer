import json
from pathlib import Path

import httpx
import pytest
import respx

from crate2discogs.db import Repo, connect
from crate2discogs.discogs import DiscogsClient, DiscogsError, DiscogsNotFound
from crate2discogs.ratelimit import TokenBucket

API = "https://api.discogs.com"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "discogs"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(tmp_path: Path, clock: FakeClock) -> DiscogsClient:
    return DiscogsClient(
        "tok",
        "me",
        "crate2discogs/test",
        repo=Repo(connect(tmp_path / "t.db")),
        cache_ttl=3600,
        bucket=TokenBucket(60, 60.0, clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
    )


def _ok(data: object = None, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=data if data is not None else {"results": []}, headers=headers)


@respx.mock
def test_auth_and_user_agent(client: DiscogsClient) -> None:
    route = respx.get(f"{API}/database/search").mock(return_value=_ok())
    client.search(barcode="4036909000014", type="release")
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Discogs token=tok"
    assert req.headers["User-Agent"] == "crate2discogs/test"
    assert req.url.params["barcode"] == "4036909000014"


def test_bucket_61st_request_waits(clock: FakeClock) -> None:
    bucket = TokenBucket(60, 60.0, clock=clock, sleep=clock.sleep)
    for _ in range(60):
        bucket.acquire()
    assert clock.sleeps == []
    bucket.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]


@respx.mock
def test_ratelimit_remaining_low_pauses(client: DiscogsClient, clock: FakeClock) -> None:
    respx.get(f"{API}/releases/1").mock(return_value=_ok({"id": 1}, **{"X-Discogs-Ratelimit-Remaining": "2"}))
    respx.get(f"{API}/releases/2").mock(return_value=_ok({"id": 2}))
    client.release(1)
    assert clock.sleeps == []
    client.release(2)
    assert clock.sleeps == [10.0]


@respx.mock
def test_429_then_200(client: DiscogsClient, clock: FakeClock) -> None:
    route = respx.get(f"{API}/releases/5").mock(side_effect=[httpx.Response(429), _ok({"id": 5})])
    assert client.release(5) == {"id": 5}
    assert route.call_count == 2
    assert clock.sleeps == [5]


@respx.mock
def test_retry_after_honoured(client: DiscogsClient, clock: FakeClock) -> None:
    respx.get(f"{API}/releases/5").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), _ok({"id": 5})]
    )
    client.release(5)
    assert clock.sleeps == [7.0]


@respx.mock
def test_three_5xx_raise_with_context(client: DiscogsClient, clock: FakeClock) -> None:
    respx.get(f"{API}/releases/9").mock(return_value=httpx.Response(502))
    with pytest.raises(DiscogsError, match=r"/releases/9.*3 attempts.*HTTP 502"):
        client.release(9)
    assert clock.sleeps == [1, 2]  # waits between the 3 attempts, none after the last


@respx.mock
def test_404_raises_not_found(client: DiscogsClient) -> None:
    respx.get(f"{API}/releases/404").mock(
        return_value=httpx.Response(404, json={"message": "Release not found."})
    )
    with pytest.raises(DiscogsNotFound):
        client.release(404)


@respx.mock
def test_search_cached_and_ttl(client: DiscogsClient, clock: FakeClock) -> None:
    route = respx.get(f"{API}/database/search").mock(return_value=_ok({"results": [{"id": 1}]}))
    a = client.search(catno="DKMNTL 031", type="release")
    b = client.search(type="release", catno="DKMNTL 031")
    assert a == b
    assert route.call_count == 1
    clock.t += 3601
    client.search(catno="DKMNTL 031", type="release")
    assert route.call_count == 2


@respx.mock
def test_release_cached(client: DiscogsClient) -> None:
    route = respx.get(f"{API}/releases/3").mock(return_value=_ok({"id": 3}))
    client.release(3)
    client.release(3)
    assert route.call_count == 1


@respx.mock
def test_collection_lookup_never_cached(client: DiscogsClient) -> None:
    route = respx.get(f"{API}/users/me/collection/releases/7").mock(
        side_effect=[_ok({"releases": []}), _ok({"releases": [{"instance_id": 1}]})]
    )
    assert client.collection_instances(7) == []
    assert client.collection_instances(7) == [{"instance_id": 1}]
    assert route.call_count == 2


@respx.mock
def test_post_never_cached_and_returns_instance_id(client: DiscogsClient) -> None:
    body = json.loads((FIXTURES / "add_release.json").read_text())
    route = respx.post(f"{API}/users/me/collection/folders/42/releases/249504").mock(
        return_value=httpx.Response(201, json=body)
    )
    assert client.add_release(42, 249504) == 1234567890
    assert client.add_release(42, 249504) == 1234567890
    assert route.call_count == 2
    assert client.repo is not None
    assert client.repo.conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


@respx.mock
def test_identity_not_cached(client: DiscogsClient) -> None:
    route = respx.get(f"{API}/oauth/identity").mock(return_value=_ok({"username": "me"}))
    client.identity()
    client.identity()
    assert route.call_count == 2


@respx.mock
def test_429_waits_up_to_a_minute_then_succeeds(client: DiscogsClient, clock: FakeClock) -> None:
    route = respx.get(f"{API}/releases/5").mock(side_effect=[httpx.Response(429)] * 5 + [_ok({"id": 5})])
    assert client.release(5) == {"id": 5}
    assert route.call_count == 6
    assert clock.sleeps == [5, 10, 20, 40, 60]


@respx.mock
def test_429_gives_up_after_about_three_minutes(client: DiscogsClient, clock: FakeClock) -> None:
    respx.get(f"{API}/releases/5").mock(return_value=httpx.Response(429))
    with pytest.raises(DiscogsError, match=r"7 attempts.*429"):
        client.release(5)
    assert sum(clock.sleeps) == 195


def test_default_pacing_is_steady_without_burst(clock: FakeClock) -> None:
    c = DiscogsClient("t", "me", "ua", clock=clock, sleep=clock.sleep)
    c.bucket.clock = clock
    c.bucket.last = clock()
    for _ in range(5):
        c.bucket.acquire()
    # First request immediate, then ~60/55 s apart: no burst of 60.
    assert clock.sleeps == [pytest.approx(60 / 55)] * 4
