"""Tool tests. Fixtures only: nothing here touches the network or a token store."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from zoho_connector import tools
from zoho_connector.cli import app
from zoho_connector.client.demo import build_demo_client
from zoho_connector.client.limits import DailyBudget
from zoho_connector.config import Settings
from zoho_connector.errors import InvalidInputError, NotFoundError
from zoho_connector.tools import mappers

FIXTURES = Path(__file__).parent / "fixtures"
ORDER_ID = "4261357000000039414"  # SO-00011, the captured detail
ITEM_ID = "4261357000000039207"  # A4 Notebook Hardcover, the captured detail


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    """Returns a canned body and records what the tool asked for."""

    def __init__(self, body: dict[str, Any], warning: bool = False) -> None:
        self.body = body
        self.warning = warning
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []

    async def get(
        self, path: str, params: dict[str, Any] | None = None, ttl: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, dict(params or {}), ttl))
        return self.body

    def quota(self) -> dict[str, Any]:
        return {"used_today": 8, "budget": 10, "remaining": 2, "warning": self.warning}


def settings() -> Settings:
    return Settings(_env_file=None, ZOHO_DEMO=True, DAILY_BUDGET=900)


@pytest.fixture
async def demo():
    async with build_demo_client(settings()) as client:
        yield client


# mappers ---------------------------------------------------------------------------------------


def test_order_summary_mapper() -> None:
    o = mappers.order_summary(load("salesorders_list")["salesorders"][1])
    assert (o.number, o.customer, o.status, o.total, o.currency) == (
        "SO-00010",
        "Carol Nguyen",
        "fulfilled",
        8.99,
        "INR",
    )
    assert o.date == "2026-10-06"
    assert o.shipment_status == "fulfilled"


def test_order_summary_blank_shipment_status_is_none() -> None:
    assert mappers.order_summary(load("salesorders_list")["salesorders"][0]).shipment_status is None


def test_order_detail_mapper_masks_contact_and_drops_free_text() -> None:
    d = mappers.order_detail(load("salesorder_detail")["salesorder"])
    assert d.number == "SO-00011"
    assert d.reference_number is None
    assert d.customer_email == "u***@example.com"
    assert d.customer_phone == "***0000"
    assert [(i.sku, i.name, i.quantity, i.rate, i.amount) for i in d.line_items] == [
        ("NOTE-A5-RUL", "A5 Notebook Ruled", 3.0, 6.5, 19.5)
    ]
    dumped = d.model_dump_json()
    assert "notes" not in dumped and "terms" not in dumped and "description" not in dumped


def test_item_summary_mapper_without_stock_fields() -> None:
    i = mappers.item_summary(load("items_list")["items"][0])
    assert (i.name, i.sku, i.status, i.unit, i.rate) == (
        "A4 Notebook Hardcover",
        "NOTE-A4-HC",
        "active",
        "pcs",
        12.0,
    )
    assert i.stock_on_hand is None and i.available_stock is None


def test_item_detail_mapper_reads_stock_and_locations() -> None:
    # The captured items do not track inventory, so the stock fields are synthetic here.
    raw = {
        **load("item_detail")["item"],
        "stock_on_hand": 12,
        "actual_available_stock": 9,
        "reorder_level": "5",
        "locations": [
            {"location_name": "Main", "location_stock_on_hand": 8, "location_available_stock": 6},
            {"location_name": "Store", "location_stock_on_hand": 4, "location_available_stock": 3},
        ],
    }
    d = mappers.item_detail(raw)
    assert (d.stock_on_hand, d.available_stock, d.reorder_level) == (12.0, 9.0, 5.0)
    assert [(loc.name, loc.stock_on_hand, loc.available_stock) for loc in d.locations] == [
        ("Main", 8.0, 6.0),
        ("Store", 4.0, 3.0),
    ]


def test_item_detail_mapper_on_captured_item() -> None:
    d = mappers.item_detail(load("item_detail")["item"])
    assert d.item_id == ITEM_ID and d.locations == [] and d.reorder_level is None


# pagination and Zoho parameter names -----------------------------------------------------------


async def test_next_page_follows_has_more_page() -> None:
    more = FakeClient(load("items_list"))  # has_more_page: true
    page = await tools.list_items(more, page=2)  # type: ignore[arg-type]
    assert page.next_page == 3
    last = FakeClient(load("salesorders_search"))  # has_more_page: false
    assert (await tools.search_sales_orders(last, "alice")).next_page is None  # type: ignore[arg-type]


async def test_list_orders_sends_zoho_params() -> None:
    fake = FakeClient(load("salesorders_list"))
    await tools.list_sales_orders(
        fake,  # type: ignore[arg-type]
        status="Void",
        date_from="2026-10-01",
        date_to="2026-10-31",
        page=2,
        per_page=10,
    )
    assert fake.calls == [
        (
            "/salesorders",
            {
                "page": 2,
                "per_page": 10,
                "status": "void",
                "date_start": "2026-10-01",
                "date_end": "2026-10-31",
            },
            None,
        )
    ]


async def test_list_orders_drops_rows_outside_the_date_window() -> None:
    fake = FakeClient(load("salesorders_list"))  # every order is dated 2026-10-06
    late = await tools.list_sales_orders(fake, date_from="2026-10-07")  # type: ignore[arg-type]
    assert late.results == []
    inside = await tools.list_sales_orders(fake, date_to="2026-10-06")  # type: ignore[arg-type]
    assert len(inside.results) == 5


async def test_search_and_item_params_and_ttl() -> None:
    fake = FakeClient(load("salesorders_search"))
    await tools.search_sales_orders(fake, "  Alice  ", status="draft")  # type: ignore[arg-type]
    assert fake.calls[-1][:2] == (
        "/salesorders",
        {"search_text": "Alice", "page": 1, "per_page": 25, "status": "draft"},
    )

    fake = FakeClient(load("items_search"))
    await tools.search_items(fake, "A4")  # type: ignore[arg-type]
    assert fake.calls[-1][:2] == ("/items", {"search_text": "A4", "page": 1, "per_page": 25})
    await tools.list_items(fake)  # type: ignore[arg-type]
    assert fake.calls[-1][1]["filter_by"] == "Status.Active"

    fake = FakeClient(load("item_detail"))
    await tools.get_item(fake, ITEM_ID)  # type: ignore[arg-type]
    assert fake.calls == [(f"/items/{ITEM_ID}", {}, 300.0)]


# validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: tools.list_sales_orders(c, date_from="2026-13-01"),
        lambda c: tools.list_sales_orders(c, date_to="yesterday"),
        lambda c: tools.list_sales_orders(c, date_from="20261006"),
        lambda c: tools.list_sales_orders(c, date_from="2026-10-31", date_to="2026-10-01"),
        lambda c: tools.list_sales_orders(c, per_page=500),
        lambda c: tools.list_sales_orders(c, per_page=0),
        lambda c: tools.list_sales_orders(c, page=0),
        lambda c: tools.list_sales_orders(c, status="open; drop"),
        lambda c: tools.search_sales_orders(c, "a"),
        lambda c: tools.search_sales_orders(c, " a "),
        lambda c: tools.search_items(c, "x"),
        lambda c: tools.list_items(c, per_page=500),
        lambda c: tools.list_items(c, status="bogus"),
        lambda c: tools.get_item(c, "../organizations"),
        lambda c: tools.get_sales_order(c, ""),
    ],
)
async def test_invalid_input_never_reaches_zoho(call: Any) -> None:
    fake = FakeClient(load("items_list"))
    with pytest.raises(InvalidInputError) as err:
        await call(fake)
    assert err.value.code == "INVALID_INPUT"
    assert fake.calls == []


# demo mode -------------------------------------------------------------------------------------


async def test_unknown_ids_raise_not_found(demo: Any) -> None:
    with pytest.raises(NotFoundError):
        await tools.get_sales_order(demo, "999")
    with pytest.raises(NotFoundError):
        await tools.get_item(demo, "999")


async def test_no_match_is_an_empty_page(demo: Any) -> None:
    page = await tools.search_items(demo, "zzzz-no-such-item")
    assert page.results == [] and page.next_page is None
    assert (await tools.search_sales_orders(demo, "zzzz")).results == []


async def test_demo_filters_search_and_pagination(demo: Any) -> None:
    void = await tools.list_sales_orders(demo, status="void")
    assert [o.number for o in void.results] == ["SO-00011"]
    alice = await tools.search_sales_orders(demo, "alice johnson")
    assert {o.customer for o in alice.results} == {"Alice Johnson"} and len(alice.results) == 4
    by_sku = await tools.search_items(demo, "note-a4")
    assert [i.sku for i in by_sku.results] == ["NOTE-A4-HC"]

    first = await tools.list_items(demo, status="all", per_page=2)
    assert len(first.results) == 2 and first.next_page == 2
    last = await tools.list_items(demo, status="all", page=3, per_page=2)
    assert len(last.results) == 1 and last.next_page is None


async def test_demo_details_match_fixtures(demo: Any) -> None:
    order = await tools.get_sales_order(demo, ORDER_ID)
    assert order.number == "SO-00011" and order.customer_email == "u***@example.com"
    # an order with no captured detail is served from its list record
    other = await tools.get_sales_order(demo, "4261357000000039365")
    assert other.number == "SO-00008" and other.line_items == []
    assert (await tools.get_item(demo, ITEM_ID)).sku == "NOTE-A4-HC"


async def test_demo_mode_makes_no_http_calls_and_uses_no_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_: Any, **__: Any) -> httpx.Response:
        raise AssertionError("demo mode must not open a real HTTP connection")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    cfg = settings()
    async with build_demo_client(cfg) as client:
        await tools.list_sales_orders(client)
        await tools.get_sales_order(client, ORDER_ID)
        await tools.search_sales_orders(client, "alice")
        await tools.list_items(client)
        await tools.get_item(client, ITEM_ID)
        await tools.search_items(client, "mug")
        status = await tools.connector_status(client, cfg)
    assert status.mode == "demo"
    assert status.used_today == 0 and status.budget == 900


async def test_budget_warning_appears_at_80_percent() -> None:
    budget = DailyBudget(10, path=None)
    for _ in range(6):
        budget.count()
    async with build_demo_client(settings(), budget=budget) as client:
        below = await tools.list_items(client)  # request 7 of 10
        assert below.warning is None
        at_limit = await tools.list_items(client, status="all")  # request 8 of 10 = 80%
        assert at_limit.warning is not None and "80%" in at_limit.warning
        assert "8 of 10" in at_limit.warning


async def test_warning_text_comes_from_quota() -> None:
    page = await tools.list_items(FakeClient(load("items_list"), warning=True))  # type: ignore[arg-type]
    assert page.warning is not None and "80%" in page.warning


# CLI -------------------------------------------------------------------------------------------


def test_cli_tools_group_in_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_DEMO", "1")
    runner = CliRunner()
    status = runner.invoke(app, ["tools", "status"])
    assert status.exit_code == 0 and json.loads(status.output)["mode"] == "demo"
    orders = runner.invoke(app, ["tools", "list-orders", "--status", "void"])
    assert [o["number"] for o in json.loads(orders.output)["results"]] == ["SO-00011"]
    bad = runner.invoke(app, ["tools", "list-items", "--per-page", "500"])
    assert bad.exit_code == 1 and "INVALID_INPUT" in bad.output
