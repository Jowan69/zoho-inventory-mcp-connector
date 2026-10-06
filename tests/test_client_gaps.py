"""Client tests that close gaps found in the audit: refresh storms, retry edge rules, budget
persistence through the client, cache lifetimes end to end, and request shape for every tool."""

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

from zoho_connector import tools
from zoho_connector.auth.oauth import TokenManager
from zoho_connector.auth.token_store import StoredTokens, TokenStore
from zoho_connector.client.cache import TTLCache
from zoho_connector.client.limits import DailyBudget, TokenBucket
from zoho_connector.client.zoho_client import MAX_RETRY_AFTER_S, ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import (
    DailyQuotaExhaustedError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
)

BASE = "https://www.zohoapis.com/inventory/v1"
ITEMS = f"{BASE}/items"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ACCESS = "1000.aaaabbbbccccddddeeeeffff00001111.22223333444455556666777788889999"
OK = {"code": 0, "message": "success", "items": []}
FIXTURES = Path(__file__).parent / "fixtures"
ORDER_ID = "4261357000000039414"
ITEM_ID = "4261357000000039207"


class FakeClock:
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
    async def get_access_token(self) -> str:
        return ACCESS

    def invalidate(self, rejected: str | None = None) -> None:
        return None


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def limited(code: int | None, **headers: str) -> httpx.Response:
    body = {"message": "Too many"} if code is None else {"code": code, "message": "Too many"}
    return httpx.Response(429, json=body, headers=headers)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    store = TokenStore(Fernet.generate_key().decode(), tmp_path / "tokens.enc")
    store.save(
        StoredTokens(
            refresh_token="1000.refresh.refresh",
            accounts_server="https://accounts.zoho.com",
            api_domain="https://www.zohoapis.com",
            org_id="987654",
            org_name="Acme",
        )
    )
    return store


@pytest.fixture
def make_client(clock: FakeClock, store: TokenStore) -> Callable[..., ZohoClient]:
    def build(budget: DailyBudget | None = None, **kwargs: Any) -> ZohoClient:
        kwargs.setdefault("token_manager", FakeTokens())
        return ZohoClient(
            Settings(_env_file=None, ZOHO_ORG_ID=None, ZOHO_CLIENT_SECRET=SecretStr("s")),
            store,
            kwargs.pop("token_manager"),
            budget=budget or DailyBudget(900, path=None),
            bucket=TokenBucket(clock=clock, sleep=clock.sleep),
            cache=TTLCache(clock=clock),
            clock=clock,
            sleep=clock.sleep,
            jitter=kwargs.pop("jitter", lambda: 0.5),
            **kwargs,
        )

    return build


# ------------------------------------------------------------------ single-flight refresh


@respx.mock
async def test_concurrent_401s_trigger_one_forced_refresh(
    store: TokenStore, tmp_path: Path
) -> None:
    """Four in-flight requests all get 401 for the same stale token, at different moments.

    Regression: each 401 used to wipe the cached token, even when a sibling request had just
    replaced it, so N requests caused N refreshes instead of one.
    """
    settings = Settings(_env_file=None, ZOHO_CLIENT_ID="cid", ZOHO_CLIENT_SECRET=SecretStr("sec"))
    refreshes = [0]

    def refresh(request: httpx.Request) -> httpx.Response:
        refreshes[0] += 1
        return httpx.Response(
            200, json={"access_token": f"1000.token{refreshes[0]}.x", "expires_in": 3600}
        )

    respx.post(TOKEN_URL).mock(side_effect=refresh)

    async def items(request: httpx.Request) -> httpx.Response:
        if "token1" in request.headers["Authorization"]:
            for _ in range(int(request.url.params["page"]) * 40):  # stagger the 401s
                await asyncio.sleep(0)
            return httpx.Response(401, json={})
        return httpx.Response(200, json=OK)

    respx.get(ITEMS).mock(side_effect=items)
    async with ZohoClient(
        settings, store, TokenManager(settings, store), budget=DailyBudget(900, path=None)
    ) as client:
        results = await asyncio.gather(*(client.get("/items", {"page": i}) for i in range(1, 5)))
    assert results == [OK] * 4
    assert refreshes[0] == 2  # the first token, plus exactly one replacement


# ------------------------------------------------------------------ retry rules, edge cases


async def test_retry_after_is_capped(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    respx_mock.get(ITEMS).mock(
        side_effect=[limited(44, **{"Retry-After": "3600"}), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        await client.get("/items")
    assert clock.sleeps == [MAX_RETRY_AFTER_S]


@pytest.mark.parametrize("header", ["soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-5"])
async def test_unusable_retry_after_falls_back_to_backoff(
    respx_mock: respx.MockRouter,
    make_client: Callable[..., ZohoClient],
    clock: FakeClock,
    header: str,
) -> None:
    respx_mock.get(ITEMS).mock(
        side_effect=[limited(44, **{"Retry-After": header}), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        await client.get("/items")
    assert clock.sleeps == [2.5]


async def test_default_jitter_stays_within_zero_to_one_second(
    respx_mock: respx.MockRouter, store: TokenStore, clock: FakeClock
) -> None:
    respx_mock.get(ITEMS).mock(side_effect=[limited(44)] * 3 + [httpx.Response(200, json=OK)])
    async with ZohoClient(
        Settings(_env_file=None),
        store,
        FakeTokens(),
        budget=DailyBudget(900, path=None),
        clock=clock,
        sleep=clock.sleep,
    ) as client:
        await client.get("/items")
    for sleep, base in zip(clock.sleeps, (2, 4, 8), strict=True):
        assert base <= sleep < base + 1


async def test_429_without_a_code_is_treated_as_per_minute(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[limited(None), httpx.Response(200, json=OK)])
    async with make_client() as client:
        assert await client.get("/items") == OK
    assert route.call_count == 2


async def test_code_45_after_code_44_stops_retrying(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).mock(side_effect=[limited(44), limited(45), limited(44)])
    async with make_client() as client:
        with pytest.raises(DailyQuotaExhaustedError):
            await client.get("/items")
    assert route.call_count == 2


async def test_rate_limited_error_is_not_cached(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[limited(44)] * 4 + [httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        with pytest.raises(RateLimitedError):
            await client.get("/items")
        assert await client.get("/items") == OK
    assert route.call_count == 5


async def test_upstream_failure_is_not_cached(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        with pytest.raises(UpstreamError):
            await client.get("/items")
        assert await client.get("/items") == OK
    assert route.call_count == 3


async def test_401_refresh_does_not_use_up_the_5xx_retry(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[
            httpx.Response(401, json={}),
            httpx.Response(500),
            httpx.Response(200, json=OK),
        ]
    )
    async with make_client() as client:
        assert await client.get("/items") == OK
    assert route.call_count == 3
    assert clock.sleeps == [2.0]


async def test_5xx_and_network_error_share_one_retry(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.get(ITEMS).mock(
        side_effect=[httpx.Response(502), httpx.ConnectError("boom"), httpx.Response(200, json=OK)]
    )
    async with make_client() as client:
        with pytest.raises(UpstreamError):
            await client.get("/items")
    assert route.call_count == 2  # CLAUDE.md: 5xx gets one retry, not one per failure kind


async def test_200_with_non_object_body_is_upstream_error(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(ITEMS).respond(200, text="<html>maintenance</html>")
    async with make_client() as client:
        with pytest.raises(UpstreamError):
            await client.get("/items")


async def test_error_message_is_truncated_and_status_is_mapped(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(ITEMS).respond(400, json={"message": "x" * 5000})
    async with make_client() as client:
        with pytest.raises(InvalidInputError) as err:
            await client.get("/items")
    assert len(err.value.message) == 300


async def test_failures_release_the_concurrency_slot(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(url__startswith=ITEMS).mock(
        side_effect=lambda r: httpx.Response(404, json={"message": "gone"})
    )
    async with make_client() as client:
        failures = await asyncio.gather(
            *(client.get("/items", {"page": i}) for i in range(12)), return_exceptions=True
        )
        assert all(isinstance(f, NotFoundError) for f in failures)
        respx_mock.get(f"{BASE}/salesorders").respond(200, json=OK)
        assert await asyncio.wait_for(client.get("/salesorders"), timeout=5) == OK


# ------------------------------------------------------------------ daily budget through the client


async def test_budget_survives_a_restart_through_the_client(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], tmp_path: Path
) -> None:
    usage = tmp_path / "usage.json"
    today = date(2026, 10, 6)
    route = respx_mock.get(ITEMS).respond(200, json=OK)

    async with make_client(budget=DailyBudget(3, usage, today=lambda: today)) as first:
        for page in range(3):
            await first.get("/items", {"page": page}, ttl=0)
    assert route.call_count == 3

    # "Restart": a new budget object and a new client, same file, same UTC day.
    async with make_client(budget=DailyBudget(3, usage, today=lambda: today)) as second:
        assert second.quota()["used_today"] == 3
        with pytest.raises(DailyQuotaExhaustedError):
            await second.get("/items", {"page": 9})
    assert route.call_count == 3  # the restarted client made no HTTP call

    # ... and the next UTC day starts fresh.
    tomorrow = date(2026, 10, 7)
    async with make_client(budget=DailyBudget(3, usage, today=lambda: tomorrow)) as third:
        await third.get("/items", {"page": 9})
    assert route.call_count == 4


def test_warning_is_logged_once_when_80_percent_is_crossed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    budget = DailyBudget(10, path=None)
    with caplog.at_level(logging.WARNING, logger="zoho_connector.client.limits"):
        for _ in range(10):
            budget.count()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["daily budget: 8 of 10 requests used (80%)"]


def test_warning_is_not_repeated_after_a_restart_past_80_percent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    usage = tmp_path / "usage.json"
    today = date(2026, 10, 6)
    first = DailyBudget(10, usage, today=lambda: today)
    for _ in range(8):
        first.count()
    caplog.clear()  # drop the warning the first process logged when it crossed 80%
    with caplog.at_level(logging.WARNING, logger="zoho_connector.client.limits"):
        restarted = DailyBudget(10, usage, today=lambda: today)
        restarted.count()
    assert restarted.is_warning()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_default_budget_is_900() -> None:
    assert Settings(_env_file=None).DAILY_BUDGET == 900


# ------------------------------------------------------------------ cache lifetimes, end to end


async def test_list_responses_are_cached_for_60_seconds(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(ITEMS).respond(200, json=fixture("items_list"))
    async with make_client() as client:
        await tools.list_items(client)
        clock.now += 59
        await tools.list_items(client)
        assert route.call_count == 1
        clock.now += 2  # 61 s after the first fetch
        await tools.list_items(client)
        assert route.call_count == 2


async def test_item_detail_is_cached_for_300_seconds(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(f"{ITEMS}/{ITEM_ID}").respond(200, json=fixture("item_detail"))
    async with make_client() as client:
        await tools.get_item(client, ITEM_ID)
        clock.now += 299
        await tools.get_item(client, ITEM_ID)
        assert route.call_count == 1
        clock.now += 2  # 301 s after the first fetch
        await tools.get_item(client, ITEM_ID)
        assert route.call_count == 2


async def test_sales_order_detail_uses_the_short_ttl(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient], clock: FakeClock
) -> None:
    route = respx_mock.get(f"{BASE}/salesorders/{ORDER_ID}").respond(
        200, json=fixture("salesorder_detail")
    )
    async with make_client() as client:
        await tools.get_sales_order(client, ORDER_ID)
        clock.now += 61
        await tools.get_sales_order(client, ORDER_ID)
    assert route.call_count == 2


async def test_cache_hits_do_not_spend_budget(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    respx_mock.get(ITEMS).respond(200, json=OK)
    async with make_client(budget=DailyBudget(10, path=None)) as client:
        for _ in range(5):
            await client.get("/items")
        assert client.quota()["used_today"] == 1


# ------------------------------------------------------------------ request shape for every tool


async def test_every_tool_sends_org_id_bearer_header_and_only_get(
    respx_mock: respx.MockRouter, make_client: Callable[..., ZohoClient]
) -> None:
    route = respx_mock.route(url__startswith=BASE).mock(
        side_effect=lambda request: httpx.Response(200, json=_body_for(request.url.path))
    )
    async with make_client() as client:
        await tools.list_sales_orders(client)
        await tools.get_sales_order(client, ORDER_ID)
        await tools.search_sales_orders(client, "alice")
        await tools.list_items(client)
        await tools.get_item(client, ITEM_ID)
        await tools.search_items(client, "notebook")
    assert route.call_count == 6
    for call in route.calls:
        request = call.request
        assert request.method == "GET"
        assert request.url.params["organization_id"] == "987654"
        assert request.headers["Authorization"] == f"Zoho-oauthtoken {ACCESS}"
        assert request.content == b""


def _body_for(path: str) -> dict[str, Any]:
    if path.endswith(f"/salesorders/{ORDER_ID}"):
        return fixture("salesorder_detail")
    if path.endswith(f"/items/{ITEM_ID}"):
        return fixture("item_detail")
    return fixture("salesorders_list" if path.endswith("/salesorders") else "items_list")
