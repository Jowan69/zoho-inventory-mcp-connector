"""Fire 150 GETs through ZohoClient with the cache off and report how the limiter behaved.

Default: a local mock that enforces 100 requests/min (HTTP 429 code 44 above it) on a simulated
clock, so it finishes instantly and never touches Zoho.
--live:  real Zoho calls (uses 150 requests of the daily quota; asks for confirmation first).

Run:  uv run python scripts/load_test.py [--live]
"""

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from zoho_connector.auth.oauth import TokenManager
from zoho_connector.auth.token_store import StoredTokens, TokenStore
from zoho_connector.client.limits import DailyBudget, TokenBucket
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings

CALLS = 150
MOCK_LIMIT_PER_MIN = 100
LIVE_PATH = "/items"


class VirtualClock:
    """Time that only moves when something sleeps, so waits cost nothing in real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class CountingHandler(logging.Handler):
    """Counts the client's request log lines by HTTP status."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.statuses: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        for part in record.getMessage().split():
            if part.startswith("status="):
                self.statuses.append(part.removeprefix("status="))


def _count_requests() -> CountingHandler:
    counter = CountingHandler()
    log = logging.getLogger("zoho_connector.client.zoho_client")
    log.setLevel(logging.INFO)
    log.propagate = False  # keep the 150 per-request lines off the console
    log.addHandler(counter)
    return counter


def _print_summary(
    succeeded: int,
    failed: int,
    handler: CountingHandler,
    waits: list[float],
    elapsed: float,
    note: str,
) -> None:
    sent = [s for s in handler.statuses if s != "cached"]
    print(f"Total calls:      {CALLS}")
    print(f"Succeeded:        {succeeded}")
    print(f"Failed:           {failed}")
    print(f"HTTP requests:    {len(sent)}")
    print(f"Retries:          {len(sent) - CALLS}")
    print(f"  429 responses:  {sum(1 for s in sent if s == '429')}")
    print(f"Total waits:      {len(waits)} ({sum(waits):.1f} s)")
    print(f"Elapsed:          {elapsed:.1f} s {note}")


async def _fire(client: ZohoClient, path: str) -> tuple[int, int]:
    results = await asyncio.gather(
        *(client.get(path, {"page": 1}, ttl=0) for _ in range(CALLS)), return_exceptions=True
    )
    failed = sum(1 for r in results if isinstance(r, BaseException))
    return CALLS - failed, failed


async def run_mock() -> None:
    clock = VirtualClock()
    window: deque[float] = deque()

    def handler(request: httpx.Request) -> httpx.Response:
        while window and clock.now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= MOCK_LIMIT_PER_MIN:
            return httpx.Response(429, json={"code": 44, "message": "Too many requests"})
        window.append(clock.now)
        return httpx.Response(200, json={"code": 0, "message": "success", "items": []})

    class MockTokens:
        async def get_access_token(self) -> str:
            return "mock-token"

        def invalidate(self) -> None:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        store = TokenStore(Fernet.generate_key().decode(), Path(tmp) / "mock.enc")
        store.save(
            StoredTokens(
                refresh_token="mock",
                accounts_server="https://accounts.zoho.com",
                api_domain="https://www.zohoapis.com",
                org_id="mock-org",
                org_name="Mock",
            )
        )
        counter = _count_requests()
        async with ZohoClient(
            Settings(_env_file=None),
            store,
            MockTokens(),
            transport=httpx.MockTransport(handler),
            budget=DailyBudget(10_000, path=None),  # in-memory: never touch tokens/usage.json
            bucket=TokenBucket(clock=clock, sleep=clock.sleep),
            clock=clock,
            sleep=clock.sleep,
        ) as client:
            succeeded, failed = await _fire(client, "/items")
    _print_summary(succeeded, failed, counter, clock.waits, clock.now, "(simulated clock)")


async def run_live() -> None:
    settings = Settings()
    store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
    counter = _count_requests()
    waits: list[float] = []
    bucket_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        waits.append(seconds)
        await bucket_sleep(seconds)

    started = time.monotonic()
    async with ZohoClient(
        settings,
        store,
        TokenManager(settings, store),
        bucket=TokenBucket(sleep=recording_sleep),
        sleep=recording_sleep,
    ) as client:
        succeeded, failed = await _fire(client, LIVE_PATH)
        print(f"Quota after run:  {client.quota()}")
    _print_summary(succeeded, failed, counter, waits, time.monotonic() - started, "(real time)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="call the real Zoho API")
    args = parser.parse_args()
    if args.live:
        print(f"WARNING: --live sends {CALLS} real requests to Zoho and uses {CALLS} of your daily")
        print("quota (free plan: 1000/day). Retries use more.")
        if input("Continue? [y/N] ").strip().lower() != "y":
            print("Aborted; no requests were sent.")
            return 1
        asyncio.run(run_live())
    else:
        asyncio.run(run_mock())
    return 0


if __name__ == "__main__":
    sys.exit(main())
