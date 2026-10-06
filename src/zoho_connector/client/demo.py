"""Demo mode (ZOHO_DEMO=1): serve the captured fixtures through ZohoClient, no network.

DemoTransport answers requests in-process from tests/fixtures/*.json. Search, status and date
filters and pagination are applied locally. build_demo_client wires it into a ZohoClient that
needs no token store and records no budget usage.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from zoho_connector.auth.token_store import StoredTokens, TokenStore
from zoho_connector.client.cache import TTLCache
from zoho_connector.client.limits import DailyBudget, TokenBucket
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import UpstreamError

DEMO_ORG_NAME = "Demo Organization"
DEFAULT_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
_API_PREFIX = "/inventory/v1"
_DEFAULT_PER_PAGE = 200
_SEARCH_FIELDS = {
    "items": ("name", "sku", "description"),
    "salesorders": ("salesorder_number", "customer_name", "reference_number"),
}
_ITEM_FILTERS = {"Status.Active": "active", "Status.Inactive": "inactive"}


def _read(directory: Path, name: str) -> dict[str, Any]:
    path = directory / f"{name}.json"
    if not path.is_file():
        raise UpstreamError(f"Demo fixture {path.name} not found in {directory}.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _union(key: str, id_key: str, *bodies: dict[str, Any]) -> list[dict[str, Any]]:
    """Records of every fixture, de-duplicated by id (first fixture wins)."""
    seen: dict[str, dict[str, Any]] = {}
    for body in bodies:
        for rec in body.get(key, []):
            seen.setdefault(str(rec.get(id_key)), rec)
    return list(seen.values())


def _int(value: str | None, default: int) -> int:
    try:
        return max(1, int(value)) if value is not None else default
    except ValueError:
        return default


class DemoTransport(httpx.AsyncBaseTransport):
    """An httpx transport that plays back fixtures instead of calling Zoho."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        directory = fixtures_dir or DEFAULT_FIXTURES
        self._orgs = _read(directory, "organizations")
        items_list = _read(directory, "items_list")
        orders_list = _read(directory, "salesorders_list")
        self._items = _union("items", "item_id", items_list, _read(directory, "items_search"))
        self._orders = _union(
            "salesorders",
            "salesorder_id",
            orders_list,
            _read(directory, "salesorders_search"),
            _read(directory, "salesorders_status"),
        )
        self._items_template = items_list.get("page_context", {})
        self._orders_template = orders_list.get("page_context", {})
        self._item_detail = _read(directory, "item_detail")
        self._order_detail = _read(directory, "salesorder_detail")
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        path = request.url.path
        start = path.find(_API_PREFIX)
        route = (path[start + len(_API_PREFIX) :] if start >= 0 else path).strip("/")
        status, body = self._route(route.split("/"), dict(request.url.params))
        return httpx.Response(status, json=body, request=request)

    def _route(self, parts: list[str], params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        head, rest = parts[0], parts[1:]
        if head == "organizations" and not rest:
            return 200, self._orgs
        if head == "items":
            return self._items_route(rest, params)
        if head == "salesorders":
            return self._orders_route(rest, params)
        return 404, {"code": 5, "message": "Invalid URL passed."}

    # items -------------------------------------------------------------------------------------

    def _items_route(self, rest: list[str], params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if rest:
            return self._detail(rest[0], "item", "item_id", self._items, self._item_detail)
        rows = self._items
        wanted = _ITEM_FILTERS.get(params.get("filter_by", ""))
        if wanted:
            rows = [r for r in rows if r.get("status") == wanted]
        elif params.get("filter_by") == "Status.Lowstock":
            rows = [r for r in rows if _is_low(r)]
        rows = _search(rows, params.get("search_text"), _SEARCH_FIELDS["items"])
        return 200, self._page("items", rows, params, self._items_template)

    # sales orders ------------------------------------------------------------------------------

    def _orders_route(self, rest: list[str], params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if rest:
            return self._detail(
                rest[0], "salesorder", "salesorder_id", self._orders, self._order_detail
            )
        rows = sorted(self._orders, key=lambda r: str(r.get("created_time")), reverse=True)
        if params.get("status"):
            rows = [r for r in rows if r.get("status") == params["status"]]
        if params.get("date_start"):
            rows = [r for r in rows if str(r.get("date")) >= params["date_start"]]
        if params.get("date_end"):
            rows = [r for r in rows if str(r.get("date")) <= params["date_end"]]
        rows = _search(rows, params.get("search_text"), _SEARCH_FIELDS["salesorders"])
        return 200, self._page("salesorders", rows, params, self._orders_template)

    # shared ------------------------------------------------------------------------------------

    @staticmethod
    def _detail(
        record_id: str,
        key: str,
        id_key: str,
        summaries: list[dict[str, Any]],
        detail_body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        if str(detail_body.get(key, {}).get(id_key)) == record_id:
            return 200, detail_body
        for summary in summaries:  # no captured detail: serve the list record instead
            if str(summary.get(id_key)) == record_id:
                return 200, {"code": 0, "message": "success", key: summary}
        return 404, {"code": 1006, "message": f"The {key} does not exist."}

    @staticmethod
    def _page(
        key: str, rows: list[dict[str, Any]], params: dict[str, str], template: dict[str, Any]
    ) -> dict[str, Any]:
        page = _int(params.get("page"), 1)
        per_page = _int(params.get("per_page"), _DEFAULT_PER_PAGE)
        window = rows[(page - 1) * per_page : page * per_page]
        context = {
            **template,
            "page": page,
            "per_page": per_page,
            "has_more_page": page * per_page < len(rows),
        }
        return {"code": 0, "message": "success", key: window, "page_context": context}


def _search(
    rows: list[dict[str, Any]], text: str | None, fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    if not text:
        return rows
    needle = text.lower()
    return [r for r in rows if any(needle in str(r.get(f, "")).lower() for f in fields)]


def _is_low(record: dict[str, Any]) -> bool:
    try:
        return float(record["stock_on_hand"]) <= float(record["reorder_level"])
    except (KeyError, TypeError, ValueError):
        return False


class _DemoStore(TokenStore):
    """Stands in for the encrypted store: nothing on disk, nothing to decrypt."""

    def __init__(self) -> None:  # deliberately skips TokenStore.__init__ (needs a key)
        self.path = Path("demo")

    def exists(self) -> bool:
        return True

    def load(self) -> StoredTokens | None:
        return StoredTokens(
            refresh_token="demo",
            accounts_server="https://accounts.zoho.com",
            api_domain="https://www.zohoapis.com",
            org_id="demo",
            org_name=DEMO_ORG_NAME,
            created_at=datetime.now(UTC),
        )


class _DemoTokens:
    async def get_access_token(self) -> str:
        return "demo"

    def invalidate(self) -> None:
        return None


class _DemoBudget(DailyBudget):
    """Demo requests are free: they are never counted and never run out."""

    def __init__(self, limit: int) -> None:
        super().__init__(limit, path=None)

    def count(self) -> None:
        return None

    def check(self) -> None:
        return None


def build_demo_client(
    settings: Settings,
    *,
    budget: DailyBudget | None = None,
    fixtures_dir: Path | None = None,
) -> ZohoClient:
    """A ZohoClient backed by DemoTransport. Pass `budget` only to test budget behavior."""
    return ZohoClient(
        settings,
        _DemoStore(),
        _DemoTokens(),
        transport=DemoTransport(fixtures_dir),
        budget=budget if budget is not None else _DemoBudget(settings.DAILY_BUDGET),
        bucket=TokenBucket(rate=10_000),
        cache=TTLCache(),
    )
