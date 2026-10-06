"""Sales order tools. Zoho: GET /salesorders and GET /salesorders/{id}."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.models import OrderDetail, OrderSummary, Page
from zoho_connector.tools._common import (
    DEFAULT_PER_PAGE,
    IsoDate,
    Paging,
    Query,
    ZohoId,
    page_extras,
    validated,
)
from zoho_connector.tools.mappers import order_detail, order_summary, record, rows

# Order statuses seen in Zoho: draft, confirmed, fulfilled, void, ... Pass through lowercase.
Status = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[A-Za-z][A-Za-z_]{0,39}$"),
]


class _ListInput(Paging):
    model_config = ConfigDict(extra="forbid")

    status: Status | None = None
    date_from: IsoDate | None = None
    date_to: IsoDate | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "_ListInput":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class _SearchInput(Paging):
    query: Query
    status: Status | None = None


class _IdInput(BaseModel):
    salesorder_id: ZohoId


async def list_sales_orders(
    client: ZohoClient,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page[OrderSummary]:
    args = validated(
        _ListInput,
        status=status,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )
    params: dict[str, str | int] = {"page": args.page, "per_page": args.per_page}
    if args.status:
        params["status"] = args.status
    if args.date_from:
        params["date_start"] = args.date_from
    if args.date_to:
        params["date_end"] = args.date_to
    body = await client.get("/salesorders", params)
    found = [order_summary(r) for r in rows(body, "salesorders")]
    # Belt and braces: never hand back orders outside the requested window, even if Zoho ignores
    # the date parameters.
    found = [
        o
        for o in found
        if (not args.date_from or o.date >= args.date_from)
        and (not args.date_to or o.date <= args.date_to)
    ]
    next_page, warning = page_extras(client, body, args.page)
    return Page[OrderSummary](results=found, next_page=next_page, warning=warning)


async def get_sales_order(client: ZohoClient, salesorder_id: str) -> OrderDetail:
    args = validated(_IdInput, salesorder_id=salesorder_id)
    body = await client.get(f"/salesorders/{args.salesorder_id}")
    return order_detail(record(body, "salesorder"))


async def search_sales_orders(
    client: ZohoClient,
    query: str,
    status: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page[OrderSummary]:
    """Search by order number, reference number or customer name (Zoho `search_text`)."""
    args = validated(_SearchInput, query=query, status=status, page=page, per_page=per_page)
    params: dict[str, str | int] = {
        "search_text": args.query,
        "page": args.page,
        "per_page": args.per_page,
    }
    if args.status:
        params["status"] = args.status
    body = await client.get("/salesorders", params)
    found = [order_summary(r) for r in rows(body, "salesorders")]
    next_page, warning = page_extras(client, body, args.page)
    return Page[OrderSummary](results=found, next_page=next_page, warning=warning)
