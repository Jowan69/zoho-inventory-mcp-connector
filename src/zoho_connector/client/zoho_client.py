"""ZohoClient: the only module that makes HTTP calls to Zoho. GET only."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, Protocol

import httpx

from zoho_connector.auth.token_store import TokenStore
from zoho_connector.client.cache import TTLCache
from zoho_connector.client.limits import DailyBudget, TokenBucket
from zoho_connector.config import Settings
from zoho_connector.errors import (
    AuthRequiredError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
)

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 4
MAX_RATE_RETRIES = 3
UPSTREAM_RETRY_DELAY_S = 2.0
MAX_BACKOFF_S = 30.0
MAX_RETRY_AFTER_S = 60.0
HTTP_TIMEOUT_S = 30.0
DEFAULT_TTL_S = 60.0
CODE_PER_MINUTE = 44
CODE_DAILY = 45
_MAX_MESSAGE_CHARS = 300


class TokenProvider(Protocol):
    """What the client needs from auth.oauth.TokenManager."""

    async def get_access_token(self) -> str: ...

    def invalidate(self) -> None: ...


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _zoho_message(body: dict[str, Any], status: int) -> str:
    message = body.get("message")
    text = message if isinstance(message, str) and message else f"Zoho returned HTTP {status}."
    return text[:_MAX_MESSAGE_CHARS]


def _zoho_code(body: dict[str, Any]) -> int | None:
    try:
        return int(body["code"])
    except (KeyError, TypeError, ValueError):
        return None


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff
    return min(seconds, MAX_RETRY_AFTER_S) if seconds >= 0 else None


def _says_missing(message: str) -> bool:
    lowered = message.lower()
    return "does not exist" in lowered or "doesn't exist" in lowered


class ZohoClient:
    """Rate-limited, cached, retrying read-only client for the Zoho Inventory API."""

    def __init__(
        self,
        settings: Settings,
        store: TokenStore,
        token_manager: TokenProvider,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        budget: DailyBudget | None = None,
        bucket: TokenBucket | None = None,
        cache: TTLCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 1.0),
    ) -> None:
        self._settings = settings
        self._store = store
        self._tokens = token_manager
        self._http = httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_S)
        self._budget = budget if budget is not None else DailyBudget(settings.DAILY_BUDGET)
        self._bucket = bucket if bucket is not None else TokenBucket(clock=clock, sleep=sleep)
        self._cache = cache if cache is not None else TTLCache(clock=clock)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._target: tuple[str, str] | None = None  # (base url, organization_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ZohoClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def quota(self) -> dict[str, Any]:
        used = self._budget.used
        return {
            "used_today": used,
            "budget": self._budget.limit,
            "remaining": max(0, self._budget.limit - used),
            "warning": self._budget.is_warning(),
        }

    def _resolve_target(self) -> tuple[str, str]:
        if self._target is None:
            stored = self._store.load()
            if stored is None:
                raise AuthRequiredError("Not logged in. Run `zoho-connector auth login`.")
            org_id = self._settings.ZOHO_ORG_ID or stored.org_id
            self._target = (f"{stored.api_domain.rstrip('/')}/inventory/v1", org_id)
        return self._target

    def _log(self, path: str, status: int | str, started: float, attempt: int, hit: bool) -> None:
        logger.info(
            "zoho request path=%s status=%s ms=%d attempt=%d cache_hit=%s used_today=%d",
            path,
            status,
            round((self._clock() - started) * 1000),
            attempt,
            hit,
            self._budget.used,
        )

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        ttl: float = DEFAULT_TTL_S,
    ) -> dict[str, Any]:
        """GET `path` (relative to /inventory/v1) and return the decoded JSON object.

        `ttl` is the cache lifetime in seconds; 0 bypasses the cache.
        """
        key = TTLCache.make_key(path, params)
        started = self._clock()
        if ttl > 0:
            cached = await self._cache.get(key)
            if cached is not None:
                self._log(path, "cached", started, 0, True)
                return cached

        self._budget.check()
        async with self._semaphore:
            body = await self._fetch(path, params, started)
        await self._cache.put(key, body, ttl)
        return body

    async def _fetch(
        self, path: str, params: Mapping[str, Any] | None, started: float
    ) -> dict[str, Any]:
        base_url, org_id = self._resolve_target()
        query = {**(params or {}), "organization_id": org_id}
        url = f"{base_url}/{path.lstrip('/')}"

        attempt = 0
        rate_retries = 0
        refreshed = False
        upstream_retried = False
        while True:
            attempt += 1
            if attempt > 1:
                self._budget.check()
            await self._bucket.acquire()
            token = await self._tokens.get_access_token()
            attempt_started = self._clock()
            try:
                resp = await self._http.get(
                    url, params=query, headers={"Authorization": f"Zoho-oauthtoken {token}"}
                )
            except httpx.HTTPError as exc:
                self._budget.count()
                self._log(path, "network_error", attempt_started, attempt, False)
                if upstream_retried:
                    raise UpstreamError(
                        f"Could not reach Zoho ({type(exc).__name__}) after a retry."
                    ) from exc
                upstream_retried = True
                await self._sleep(UPSTREAM_RETRY_DELAY_S)
                continue
            self._budget.count()
            self._log(path, resp.status_code, attempt_started, attempt, False)

            status = resp.status_code
            if status == 200:
                body = _json_body(resp)
                if not body:
                    raise UpstreamError("Zoho returned a 200 response that is not a JSON object.")
                return body

            body = _json_body(resp)
            message = _zoho_message(body, status)

            if status == 429:
                if _zoho_code(body) == CODE_DAILY:
                    raise DailyQuotaExhaustedError(
                        "Zoho's daily API limit is reached; it resets at midnight."
                    )
                if rate_retries >= MAX_RATE_RETRIES:
                    raise RateLimitedError(
                        f"Zoho kept rate limiting after {MAX_RATE_RETRIES} retries."
                    )
                rate_retries += 1
                delay = _retry_after(resp)
                if delay is None:
                    delay = min(2**rate_retries, MAX_BACKOFF_S) + self._jitter()
                await self._sleep(delay)
            elif status == 401:
                if refreshed:
                    raise AuthRequiredError("Zoho rejected the access token even after a refresh.")
                refreshed = True
                self._tokens.invalidate()
            elif status >= 500:
                if upstream_retried:
                    raise UpstreamError(f"Zoho failed with HTTP {status} after a retry.")
                upstream_retried = True
                await self._sleep(UPSTREAM_RETRY_DELAY_S)
            elif status == 404 or (400 <= status < 500 and _says_missing(message)):
                raise NotFoundError(message)
            elif 400 <= status < 500:
                raise InvalidInputError(message)
            else:
                raise UpstreamError(f"Unexpected HTTP {status} from Zoho.")
