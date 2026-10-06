"""Small async-safe TTL cache for successful GET responses."""

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

MAX_ENTRIES = 512


class TTLCache:
    """Maps request keys to response dicts for a limited time; the oldest entry is dropped first."""

    def __init__(
        self, max_entries: int = MAX_ENTRIES, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_entries
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(path: str, params: Mapping[str, Any] | None = None) -> str:
        """Key = path + sorted query params."""
        pairs = sorted(httpx.QueryParams(params or {}).multi_items())
        return f"{path}?{urlencode(pairs)}"

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._items[key]
                return None
            return copy.deepcopy(value)

    async def put(self, key: str, value: dict[str, Any], ttl: float) -> None:
        """Store a successful response. Callers must never pass errors."""
        if ttl <= 0:
            return
        async with self._lock:
            self._items[key] = (self._clock() + ttl, copy.deepcopy(value))
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()
