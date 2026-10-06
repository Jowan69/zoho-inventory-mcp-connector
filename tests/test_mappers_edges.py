"""Mapper and pagination edge cases: odd Zoho payloads must degrade, never crash or leak."""

from typing import Any

import pytest

from zoho_connector import tools
from zoho_connector.errors import UpstreamError
from zoho_connector.tools import mappers


class Canned:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    async def get(self, *_: Any, **__: Any) -> dict[str, Any]:
        return self.body

    def quota(self) -> dict[str, Any]:
        return {"used_today": 0, "budget": 900, "remaining": 900, "warning": False}


def test_order_summary_survives_an_empty_record() -> None:
    o = mappers.order_summary({})
    assert (o.salesorder_id, o.number, o.customer, o.total) == ("", "", "", 0.0)
    assert o.shipment_status is None


def test_order_summary_coerces_numeric_strings_and_ids() -> None:
    o = mappers.order_summary({"salesorder_id": 123, "total": "19.50", "date": "2026-10-06"})
    assert (o.salesorder_id, o.total) == ("123", 19.5)


def test_order_detail_drops_unknown_and_free_text_fields() -> None:
    raw = {
        "salesorder_id": "1",
        "notes": "call me on 555",
        "terms": "net 30",
        "billing_address": {"address": "1 Main St"},
        "custom_fields": [{"value": "secret"}],
        "line_items": [
            {
                "sku": "A",
                "name": "N",
                "quantity": 1,
                "rate": 2,
                "item_total": 2,
                "description": "free text",
            }
        ],
    }
    dumped = mappers.order_detail(raw).model_dump_json()
    for leaked in ("call me", "net 30", "Main St", "secret", "free text"):
        assert leaked not in dumped


def test_order_detail_email_falls_back_to_associated_contact_and_is_masked() -> None:
    raw = {"contact_persons_associated": [{"contact_person_email": "zoe@example.org"}]}
    assert mappers.order_detail(raw).customer_email == "z***@example.org"


def test_order_detail_never_returns_raw_contact_details() -> None:
    raw = {
        "contact_person_details": [{"email": "jo.smith@example.com", "phone": "+1 555 010 9999"}],
        "email": "other@example.com",
    }
    detail = mappers.order_detail(raw)
    dumped = detail.model_dump_json()
    assert "jo.smith" not in dumped and "555 010" not in dumped and "other@" not in dumped
    assert detail.customer_email == "j***@example.com" and detail.customer_phone == "***9999"


def test_order_detail_ignores_non_dict_line_items() -> None:
    assert mappers.order_detail({"line_items": ["x", None, 3]}).line_items == []


def test_item_stock_prefers_available_stock_over_actual_available_stock() -> None:
    both = mappers.item_summary({"available_stock": 4, "actual_available_stock": 9})
    only_actual = mappers.item_summary({"actual_available_stock": 9})
    neither = mappers.item_summary({})
    assert (both.available_stock, only_actual.available_stock, neither.available_stock) == (
        4.0,
        9.0,
        None,
    )


def test_item_summary_zero_stock_is_zero_not_none() -> None:
    item = mappers.item_summary({"stock_on_hand": 0, "available_stock": "0", "rate": 0})
    assert (item.stock_on_hand, item.available_stock, item.rate) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("junk", ["", "n/a", True, None, [], {}])
def test_item_numbers_ignore_junk(junk: Any) -> None:
    assert mappers.item_summary({"rate": junk, "stock_on_hand": junk}).rate is None


def test_item_detail_location_name_fallback() -> None:
    d = mappers.item_detail({"locations": [{"name": "Dock"}, "not-a-dict"]})
    assert [loc.name for loc in d.locations] == ["Dock"]


def test_rows_and_record_report_unexpected_shapes_as_upstream_errors() -> None:
    with pytest.raises(UpstreamError):
        mappers.rows({"code": 0}, "items")
    with pytest.raises(UpstreamError):
        mappers.record({"item": []}, "item")
    assert mappers.rows({"items": [{"a": 1}, "junk"]}, "items") == [{"a": 1}]


# ------------------------------------------------------------------ pagination


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"has_more_page": True}, 3),
        ({"has_more_page": False}, None),
        ({}, None),
        (None, None),
        ("junk", None),
    ],
)
async def test_next_page_for_every_list_tool(context: Any, expected: int | None) -> None:
    body: dict[str, Any] = {"items": [], "salesorders": []}
    if context is not None:
        body["page_context"] = context
    client: Any = Canned(body)
    assert (await tools.list_items(client, page=2)).next_page == expected
    assert (await tools.search_items(client, "ab", page=2)).next_page == expected
    assert (await tools.list_sales_orders(client, page=2)).next_page == expected
    assert (await tools.search_sales_orders(client, "ab", page=2)).next_page == expected


async def test_list_tools_report_an_upstream_error_when_the_list_is_missing() -> None:
    with pytest.raises(UpstreamError):
        await tools.list_items(Canned({"code": 0}))  # type: ignore[arg-type]
    with pytest.raises(UpstreamError):
        await tools.get_item(Canned({"code": 0}), "1")  # type: ignore[arg-type]
