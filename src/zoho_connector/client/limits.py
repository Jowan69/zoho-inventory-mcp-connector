"""Client-side rate limiting: a token bucket per minute and a persisted daily request budget."""

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path

from zoho_connector.errors import DailyQuotaExhaustedError

logger = logging.getLogger(__name__)

DEFAULT_USAGE_PATH = Path("tokens") / "usage.json"
WARN_FRACTION = 0.8
_EPSILON = 1e-9


class TokenBucket:
    """Allows `rate` calls per `per` seconds (continuous refill, starts full)."""

    def __init__(
        self,
        rate: int = 90,
        per: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._capacity = float(rate)
        self._refill_per_s = rate / per
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(rate)
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_s)
        self._updated = now

    async def acquire(self) -> None:
        """Take one token, sleeping until one is available. Waiters are served in FIFO order."""
        async with self._lock:
            waited = 0.0
            self._refill()
            while self._tokens < 1.0 - _EPSILON:
                delay = (1.0 - self._tokens) / self._refill_per_s
                await self._sleep(delay)
                waited += delay
                self._refill()
            self._tokens -= 1.0
        if waited > 0:
            logger.info("rate limiter: bucket empty, waited %.2f s", waited)


def _utc_today() -> date:
    return datetime.now(UTC).date()


class DailyBudget:
    """Counts Zoho requests per UTC day; the count survives restarts via a small JSON file."""

    def __init__(
        self,
        limit: int,
        path: Path | None = DEFAULT_USAGE_PATH,
        *,
        today: Callable[[], date] = _utc_today,
    ) -> None:
        self.limit = limit
        self._path = path
        self._today = today
        self._date = today()
        self._count = 0
        self._warned = False
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            saved_date = date.fromisoformat(data["date"])
            saved_count = int(data["count"])
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("daily budget: %s is unreadable, starting from 0", self._path)
            return
        if saved_date == self._date:
            self._count = saved_count
            self._warned = self.is_warning()

    def _save(self) -> None:
        if self._path is None:
            return
        payload = json.dumps({"date": self._date.isoformat(), "count": self._count})
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".usage.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp_name, self._path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError:
            logger.warning("daily budget: could not persist usage to %s", self._path)

    def _roll(self) -> None:
        today = self._today()
        if today != self._date:
            self._date, self._count, self._warned = today, 0, False

    @property
    def used(self) -> int:
        self._roll()
        return self._count

    def count(self) -> None:
        """Record one request sent to Zoho."""
        self._roll()
        self._count += 1
        self._save()
        if not self._warned and self.is_warning():
            self._warned = True
            logger.warning(
                "daily budget: %d of %d requests used (%.0f%%)",
                self._count,
                self.limit,
                100 * self._count / self.limit,
            )

    def check(self) -> None:
        """Raise DailyQuotaExhaustedError if the budget is spent."""
        self._roll()
        if self._count >= self.limit:
            raise DailyQuotaExhaustedError(
                f"Daily request budget of {self.limit} is used up; it resets at 00:00 UTC."
            )

    def is_warning(self) -> bool:
        self._roll()
        return self._count >= WARN_FRACTION * self.limit
