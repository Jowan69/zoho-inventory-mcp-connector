"""Map raw Zoho JSON to the small output models. Unknown fields are dropped on purpose."""

from collections.abc import Mapping
from typing import Any

from zoho_connector.errors import UpstreamError
from zoho_connector.models import (
    ItemDetail,
    ItemLocation,
    ItemSummary,
    OrderDetail,
    OrderLine,
    OrderSummary,
)
from zoho_connector.tools.masking import mask_email, mask_phone

Raw = Mapping[str, Any]


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num0(value: Any) -> float:
    number = _num(value)
    return 0.0 if number is None else number


def _dicts(value: Any) -> list[Raw]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def rows(body: Raw, key: str) -> list[Raw]:
    """The list under `key` in a list response; a missing key is an upstream shape problem."""
    if key not in body:
        raise UpstreamError(f"Unexpected Zoho response: no '{key}' list.")
    return _dicts(body[key])


def record(body: Raw, key: str) -> Raw:
    value = body.get(key)
    if not isinstance(value, dict):
        raise UpstreamError(f"Unexpected Zoho response: no '{key}' object.")
    return value


def order_summary(raw: Raw) -> OrderSummary:
    return OrderSummary(
        salesorder_id=_str(raw.get("salesorder_id")),
        number=_str(raw.get("salesorder_number")),
        date=_str(raw.get("date")),
        customer=_str(raw.get("customer_name")),
        status=_str(raw.get("status")),
        total=_num0(raw.get("total")),
        currency=_str(raw.get("currency_code")),
        shipment_status=_str(raw.get("shipped_status")) or None,
    )


def _contact(raw: Raw, keys: tuple[str, ...]) -> str | None:
    """First non-empty value of `keys` across the order's contact persons, then the order."""
    for person in _dicts(raw.get("contact_person_details")):
        for key in keys:
            if person.get(key):
                return _str(person[key])
    for key in keys:
        if raw.get(key):
            return _str(raw[key])
    return None


def order_detail(raw: Raw) -> OrderDetail:
    email = _contact(raw, ("email",))
    if email is None:
        for person in _dicts(raw.get("contact_persons_associated")):
            if person.get("contact_person_email"):
                email = _str(person["contact_person_email"])
                break
    return OrderDetail(
        **order_summary(raw).model_dump(),
        reference_number=_str(raw.get("reference_number")) or None,
        line_items=[
            OrderLine(
                sku=_str(line.get("sku")),
                name=_str(line.get("name")),
                quantity=_num0(line.get("quantity")),
                rate=_num0(line.get("rate")),
                amount=_num0(line.get("item_total")),
            )
            for line in _dicts(raw.get("line_items"))
        ],
        customer_email=mask_email(email),
        customer_phone=mask_phone(_contact(raw, ("phone", "mobile"))),
    )


def _available(raw: Raw) -> float | None:
    available = _num(raw.get("available_stock"))
    return available if available is not None else _num(raw.get("actual_available_stock"))


def item_summary(raw: Raw) -> ItemSummary:
    return ItemSummary(
        item_id=_str(raw.get("item_id")),
        name=_str(raw.get("name")),
        sku=_str(raw.get("sku")),
        status=_str(raw.get("status")),
        unit=_str(raw.get("unit")),
        rate=_num(raw.get("rate")),
        stock_on_hand=_num(raw.get("stock_on_hand")),
        available_stock=_available(raw),
    )


def item_detail(raw: Raw) -> ItemDetail:
    return ItemDetail(
        **item_summary(raw).model_dump(),
        reorder_level=_num(raw.get("reorder_level")),
        locations=[
            ItemLocation(
                name=_str(loc.get("location_name") or loc.get("name")),
                stock_on_hand=_num(loc.get("location_stock_on_hand")),
                available_stock=_num(loc.get("location_available_stock")),
            )
            for loc in _dicts(raw.get("locations"))
        ],
    )
