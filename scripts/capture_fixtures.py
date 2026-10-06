"""Capture a small set of real Zoho GET responses as sanitized test fixtures.

Makes at most 8 GET calls through ZohoClient, scrubs emails, phones and street addresses, and
writes tests/fixtures/<name>.json. Needs a prior `zoho-connector auth login`.

Run:  uv run python scripts/capture_fixtures.py
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from zoho_connector.auth.oauth import TokenManager
from zoho_connector.auth.token_store import TokenStore
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import ConnectorError

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MAX_CALLS = 8
SAMPLE_PER_PAGE = 5

EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
ADDRESS_KEYS = {"address", "street", "street2", "street_address", "attention"}


def scrub(value: Any, key: str = "") -> Any:
    """Replace emails, phones and street addresses anywhere in a decoded JSON value."""
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str) and value:
        lowered = key.lower()
        if lowered in ADDRESS_KEYS:
            return "REDACTED"
        if "phone" in lowered or lowered in {"mobile", "fax"}:
            return "0000000000"
        return EMAIL_RE.sub("user@example.com", value)
    return value


class Capture:
    def __init__(self, client: ZohoClient) -> None:
        self._client = client
        self.calls = 0

    async def get(self, name: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.calls >= MAX_CALLS:
            raise RuntimeError("call cap reached")
        self.calls += 1
        body = await self._client.get(path, params, ttl=0)
        FIXTURES.mkdir(parents=True, exist_ok=True)
        out = FIXTURES / f"{name}.json"
        out.write_text(json.dumps(scrub(body), indent=2, ensure_ascii=False) + "\n", "utf-8")
        print(f"  {self.calls}. GET {path} {params or ''} -> {out.name}")
        return body


def _first(body: dict[str, Any], key: str) -> dict[str, Any]:
    rows = body.get(key)
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise RuntimeError(f"no '{key}' in response; nothing to sample from")
    return rows[0]


async def run(settings: Settings) -> int:
    store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
    async with ZohoClient(settings, store, TokenManager(settings, store)) as client:
        cap = Capture(client)
        page = {"page": 1, "per_page": SAMPLE_PER_PAGE}
        await cap.get("organizations", "/organizations")

        items = await cap.get("items_list", "/items", page)
        item = _first(items, "items")
        await cap.get("item_detail", f"/items/{item['item_id']}")
        word = str(item.get("name", "")).split()[0]
        await cap.get("items_search", "/items", {**page, "search_text": word})

        orders = await cap.get("salesorders_list", "/salesorders", page)
        order = _first(orders, "salesorders")
        await cap.get("salesorder_detail", f"/salesorders/{order['salesorder_id']}")
        await cap.get(
            "salesorders_search",
            "/salesorders",
            {**page, "search_text": str(order.get("customer_name", ""))},
        )
        await cap.get(
            "salesorders_status", "/salesorders", {**page, "status": str(order.get("status", ""))}
        )
        return cap.calls


def main() -> None:
    try:
        calls = asyncio.run(run(Settings()))
    except ConnectorError as exc:
        raise SystemExit(f"[{exc.code}] {exc.message}") from exc
    print(f"Zoho GET calls made: {calls} (max {MAX_CALLS})")


if __name__ == "__main__":
    main()
