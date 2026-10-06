"""Pydantic v2 models for items, sales orders, pagination and tool outputs.

Deliberately small. Free-text fields (notes, terms, descriptions) are untrusted and left out.
"""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class OrderSummary(BaseModel):
    salesorder_id: str
    number: str
    date: str  # ISO 8601, YYYY-MM-DD
    customer: str
    status: str
    total: float
    currency: str
    shipment_status: str | None = None


class OrderLine(BaseModel):
    sku: str
    name: str
    quantity: float
    rate: float
    amount: float


class OrderDetail(OrderSummary):
    reference_number: str | None = None
    line_items: list[OrderLine] = []
    customer_email: str | None = None  # masked
    customer_phone: str | None = None  # masked


class ItemSummary(BaseModel):
    item_id: str
    name: str
    sku: str
    status: str
    unit: str
    rate: float | None = None
    stock_on_hand: float | None = None
    available_stock: float | None = None


class ItemLocation(BaseModel):
    name: str
    stock_on_hand: float | None = None
    available_stock: float | None = None


class ItemDetail(ItemSummary):
    reorder_level: float | None = None
    locations: list[ItemLocation] = []


class Page(BaseModel, Generic[T]):
    results: list[T]
    next_page: int | None = None
    warning: str | None = None  # set when today's request budget is >= 80% used


class ConnectorStatus(BaseModel):
    mode: Literal["live", "demo"]
    authenticated: bool
    org_name: str | None = None
    used_today: int
    budget: int
