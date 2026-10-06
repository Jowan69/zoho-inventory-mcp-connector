import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import SecretStr

from zoho_connector.auth.token_store import StoredTokens, TokenStore
from zoho_connector.client.cache import TTLCache
from zoho_connector.client.limits import DailyBudget, TokenBucket
from zoho_connector.client.zoho_client import MAX_CONCURRENCY, ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import (
    AuthRequiredError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
)

BASE = "https://www.zohoapis.com/inventory/v1"
ITEMS = f"{BASE}/items"
ACCESS = "1000.aaaabbbbccccddddeeeeffff00001111.22223333444455556666777788889999"
OK = {"code": 0, "message": "success", "items": []}


class FakeClock:
    """Monotonic clock whose sleep() just advances time; nothing really sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeTokens:
    """Stands in for TokenManager: counts refreshes without touching accounts.zoho.com."""

    def __init__(self) -> None:
        self.refreshes = 0
        self.invalidations = 0
        self._valid = False

    async def get_access_token(self) -> str:
        if not self._valid:
            self.refreshes += 1
            self._valid = True
        return ACCESS

    def invalidate(self, rejected: str | None = None) -> None:
        self.invalidations += 1
        self._valid = False


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tokens() -> FakeTokens:
    return FakeTokens()


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    store = TokenStore(Fernet.generate_key().decode(), tmp_path / "tokens.enc")
    store.save(
        StoredTokens(
            refresh_token="1000.refresh",
            accounts_server="https://accounts.zoho.com",
            api_domain="https://www.zohoapis.com",
            org_id="987654",
            org_name="Acme",
        )
    )
    return store


@pytest.fixture
def make_client(
    clock: FakeClock, tokens: FakeTokens, store: TokenStore
) -> Callable[..., ZohoClient]:
    def build(budget: DailyBudget | None = None, **kwargs: Any) -> ZohoClient:
        return ZohoClient(
            Settings(_env_file=None, ZOHO_ORG_ID=None, ZOHO_CLIENT_SECRET=SecretStr("s")),
            store,
            tokens,
            budget=budget or DailyBudget(900, path=None),
            bucket=TokenBucket(clock=clock, sleep=clock.sleep),
            cache=TTLCache(clock=clock),
            clock=clock,
            sleep=clock.sleep,
            jitter=lambda: 0.5,
            **kwargs,
        )

    return build


def rate_limited(code: int, **headers: str) -> httpx.Response:
    return httpx.Response(429, json={"code": code, "message": "Too many"}, headers=headers)


# ------------------------------------------------------------------ token bucket


async def test_bucket_allows_90_then_waits(clock: FakeClock) -> None:
    bucket = TokenBucket(clock=clock, sleep=clock.sleep)
    for _ in range(90):
        await bucket.acquire()
    assert clock.sleeps == []
    await bucket.acquire()
    assert clock.sleeps == [pytest.approx(60 / 90)]


async def test_bucket_logs_wait(clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    bucket = TokenBucket(rate=1, per=60.0, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    with caplog.at_level(logging.INFO, logger="zoho_connector.client.limits"):
        await bucket.acquire()
    assert "rate limiter: bucket empty, waited 60.00 s" in caplog.text


async def test_bucket_refills_over_time(clock: FakeClock) -> None:
    bucket = TokenBucket(rate=2, per=60.0, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    await bucket.acquire()
    clock.now += 60
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps == []


# ------------------------------------------------------------------ daily budget


def test_budget_blocks_at_limit_and_warns_at_80_percent() -> None:
    budget = DailyBudget(10, path=None)
    for _ in range(7):
        budget.count()
    assert not budget.is_warning()
    budget.count()
    assert budget.is_warning()
    budget.check()
    budget.count()
    budget.count()
    with pytest.raises(DailyQuotaExhaustedError):
        budget.check()


def test_budget_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    today = date(2026, 10, 6)
    first = DailyBudget(900, path, today=lambda: today)
    first.count()
    first.count()
    assert json.loads(path.read_text()) == {"date": "2026-10-06", "count": 2}
    assert DailyBudget(900, path, today=lambda: today).used == 2


def test_budget_resets_when_utc_date_changes(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    day = [date(2026, 10, 6)]
    budget = DailyBudget(2, path, today=lambda: day[0])
    budget.count()
    budget.count()
    with pytest.raises(DailyQuotaExhaustedError):
        budget.check()
    day[0] = date(2026, 10, 7)
    budget.check()
    assert budget.used == 0
    assert DailyBudget(2, path, today=lambda: day[0]).used == 0


def test_budget_ignores_stale_file_from_another_day(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text('{"date": "2026-10-05", "count": 899}')
    assert DailyBudget(900, path, today=lambda: date(2026, 10, 6)).used == 0


def test_budget_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text("not json")
    assert DailyBudget(900, path).used == 0


# ------------------------------------------------------------------ cache


async def test_cache_expires_and_evicts_oldest(clock: FakeClock) -> None:
    cache = TTLCache(max_entries=2, clock=clock)
    await cache.put("a", {"v": 1}, ttl=60)
    await cache.put("b", {"v": 2}, ttl=60)
    await cache.put("c", {"v": 3}, ttl=60)
    assert await cache.get("a") is None
    assert await cache.get("b") == {"v": 2}
    clock.now += 61
    assert await cache.get("b") is None
    await cache.put("d", {"v": 4}, ttl=60)
    await cache.clear()
    assert await cache.get("d") is None


async def test_cache_returns_copies(clock: FakeClock) -> None:
    cache = TTLCache(clock=clock)
    await cache.put("k", {"items": [1]}, ttl=60)
    first = await cache.get("k")
    assert first is not None
    first["items"].append(2)
    assert await cache.get("k") == {"items": [1]}


def test_cache_key_sorts_params() -> None:
    assert TTLCache.make_key("/items", {"b": 2, "a": 1}) == TTLCache.make_key(
        "/items", {"a": 1, "b": 2}
    )
    assert TTLCache.make_key("/items", {"a": 1}) != TTLCache.make_key("/salesorders", {"a": 1})


# ------------------------------------------------------------------ client: rate limiting


async def test_150_calls_all_succeed_with_bucket_waits(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).respond(200, json=OK)
    async with make_client() as client:
        for _ in range(150):
            assert await client.get("/items", ttl=0) == OK
    assert route.call_count == 150
    assert len(clock.sleeps) == 60  # the first 90 calls are free
    assert sum(clock.sleeps) == pytest.approx(60 * 60 / 90)


async def test_429_code_44_then_200_retries_once(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[rate_limited(44), httpx.Response(200, json=OK)])
    async with make_client() as client:
        assert await client.get("/items") == OK
    assert route.call_count == 2
    assert clock.sleeps == [2.5]  # min(2**1, 30) + jitter 0.5


async def test_429_respects_retry_after(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    respx_mock.get(ITEMS).mock(
        side_effect=[rate_limited(44, **{"Retry-After": "7"}), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        await client.get("/items")
    assert clock.sleeps == [7.0]


async def test_429_code_44_four_times_raises_rate_limited(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[rate_limited(44)] * 4)
    async with make_client() as client:
        with pytest.raises(RateLimitedError):
            await client.get("/items")
    assert route.call_count == 4  # first try + 3 retries
    assert clock.sleeps == [2.5, 4.5, 8.5]


async def test_429_code_45_is_final(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[rate_limited(45)])
    async with make_client() as client:
        with pytest.raises(DailyQuotaExhaustedError):
            await client.get("/items")
    assert route.call_count == 1
    assert clock.sleeps == []


async def test_budget_at_limit_makes_no_http_call(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).respond(200, json=OK)
    budget = DailyBudget(2, path=None)
    budget.count()
    budget.count()
    async with make_client(budget=budget) as client:
        with pytest.raises(DailyQuotaExhaustedError):
            await client.get("/items")
    assert route.call_count == 0


async def test_every_attempt_counts_against_budget(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(ITEMS).mock(side_effect=[rate_limited(44), httpx.Response(200, json=OK)])
    async with make_client(budget=DailyBudget(10, path=None)) as client:
        await client.get("/items")
        assert client.quota() == {
            "used_today": 2,
            "budget": 10,
            "remaining": 8,
            "warning": False,
        }


async def test_quota_warning_flag(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(ITEMS).respond(200, json=OK)
    async with make_client(budget=DailyBudget(5, path=None)) as client:
        for _ in range(4):
            await client.get("/items", ttl=0)
        assert client.quota()["warning"] is True


# ------------------------------------------------------------------ client: auth and upstream


async def test_401_then_200_refreshes_once(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], tokens: FakeTokens
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[httpx.Response(401, json={"code": 57}), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        assert await client.get("/items") == OK
    assert route.call_count == 2
    assert tokens.invalidations == 1
    assert tokens.refreshes == 2  # initial fetch + the forced refresh


async def test_401_twice_raises_auth_required(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], tokens: FakeTokens
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[httpx.Response(401, json={})] * 2)
    async with make_client() as client:
        with pytest.raises(AuthRequiredError):
            await client.get("/items")
    assert route.call_count == 2
    assert tokens.invalidations == 1


async def test_500_then_200_succeeds(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        assert await client.get("/items") == OK
    assert route.call_count == 2
    assert clock.sleeps == [2.0]


async def test_500_twice_raises_upstream_error(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[httpx.Response(500), httpx.Response(503)])
    async with make_client() as client:
        with pytest.raises(UpstreamError):
            await client.get("/items")
    assert route.call_count == 2


async def test_network_error_retries_once(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        assert await client.get("/items") == OK
    assert route.call_count == 2
    assert clock.sleeps == [2.0]


async def test_network_error_twice_raises_upstream_error(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(ITEMS).mock(side_effect=httpx.ConnectError("boom"))
    async with make_client() as client:
        with pytest.raises(UpstreamError):
            await client.get("/items")


# ------------------------------------------------------------------ client: 4xx mapping


async def test_404_is_not_found(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(f"{BASE}/items/1").respond(404, json={"code": 1002, "message": "No such item"})
    async with make_client() as client:
        with pytest.raises(NotFoundError):
            await client.get("/items/1")


async def test_message_saying_resource_does_not_exist_is_not_found(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(f"{BASE}/salesorders/1").respond(
        400, json={"code": 2006, "message": "The sales order does not exist."}
    )
    async with make_client() as client:
        with pytest.raises(NotFoundError):
            await client.get("/salesorders/1")


async def test_other_4xx_is_invalid_input_with_zoho_message(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).respond(
        400, json={"code": 2, "message": "Invalid value passed for per_page"}
    )
    async with make_client() as client:
        with pytest.raises(InvalidInputError, match="Invalid value passed for per_page"):
            await client.get("/items", {"per_page": "x"})
    assert route.call_count == 1


# ------------------------------------------------------------------ client: cache, params, logging


async def test_cache_hit_makes_no_second_http_call(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).respond(200, json=OK)
    async with make_client() as client:
        first = await client.get("/items", {"page": 1})
        second = await client.get("/items", {"page": 1})
        assert first == second == OK
        assert route.call_count == 1
        await client.get("/items", {"page": 2})
        assert route.call_count == 2
        clock.now += 61
        await client.get("/items", {"page": 1})
        assert route.call_count == 3


async def test_ttl_zero_bypasses_cache(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).respond(200, json=OK)
    async with make_client() as client:
        await client.get("/items", ttl=0)
        await client.get("/items", ttl=0)
    assert route.call_count == 2


async def test_errors_are_not_cached(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[httpx.Response(404, json={"message": "gone"}), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        with pytest.raises(NotFoundError):
            await client.get("/items")
        assert await client.get("/items") == OK
    assert route.call_count == 2


async def test_organization_id_and_auth_header_on_every_request(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(url__startswith=BASE).respond(200, json=OK)
    async with make_client() as client:
        await client.get("/items", {"page": 2, "organization_id": "evil"})
        await client.get("/salesorders")
        await client.get("/organizations")
    assert route.call_count == 3
    for call in route.calls:
        assert call.request.url.params["organization_id"] == "987654"
        assert call.request.headers["Authorization"] == f"Zoho-oauthtoken {ACCESS}"
    assert route.calls[0].request.url.params["page"] == "2"


async def test_settings_org_id_overrides_stored_one(
    respx_mock: respx.MockRouter, tokens: FakeTokens, store: TokenStore, clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).respond(200, json=OK)
    settings = Settings(_env_file=None, ZOHO_ORG_ID="555")
    async with ZohoClient(
        settings, store, tokens, budget=DailyBudget(900, path=None), clock=clock, sleep=clock.sleep
    ) as client:
        await client.get("/items")
    assert route.calls.last.request.url.params["organization_id"] == "555"


async def test_log_line_has_no_token_or_body(
    respx_mock: respx.MockRouter,
    make_client: Callable[..., ZohoClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx_mock.get(ITEMS).respond(200, json={**OK, "secret_field": "SENSITIVE-BODY"})
    with caplog.at_level(logging.INFO, logger="zoho_connector.client.zoho_client"):
        async with make_client() as client:
            await client.get("/items")
            await client.get("/items")
    lines = [r.getMessage() for r in caplog.records if r.name.endswith("zoho_client")]
    assert (
        lines[0]
        == "zoho request path=/items status=200 ms=0 attempt=1 cache_hit=False used_today=1"
    )
    assert "cache_hit=True" in lines[1]
    assert ACCESS not in caplog.text
    assert "SENSITIVE-BODY" not in caplog.text


async def test_client_exposes_only_get() -> None:
    public = {n for n in dir(ZohoClient) if not n.startswith("_")}
    assert not public & {"post", "put", "patch", "delete", "request", "send"}


# ------------------------------------------------------------------ client: concurrency


async def test_concurrency_never_exceeds_four(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    state = {"in_flight": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        for _ in range(5):
            await asyncio.sleep(0)
        state["in_flight"] -= 1
        return httpx.Response(200, json=OK)

    respx_mock.get(ITEMS).mock(side_effect=handler)
    async with make_client() as client:
        results = await asyncio.gather(*(client.get("/items", {"page": i}) for i in range(30)))
    assert len(results) == 30
    assert state["peak"] == MAX_CONCURRENCY
