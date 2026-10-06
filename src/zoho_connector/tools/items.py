"""Item tools. Zoho: GET /items and GET /items/{id}."""

from typing import Literal

from pydantic import BaseModel

from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.models import ItemDetail, ItemSummary, Page
from zoho_connector.tools._common import (
    DEFAULT_PER_PAGE,
    Paging,
    Query,
    ZohoId,
    page_extras,
    validated,
)
from zoho_connector.tools.mappers import item_detail, item_summary, record, rows

ITEM_DETAIL_TTL_S = 300.0

# Tool-facing status -> Zoho `filter_by` value.
FILTER_BY = {
    "active": "Status.Active",
    "inactive": "Status.Inactive",
    "all": "Status.All",
    "lowstock": "Status.Lowstock",
}


class _ListInput(Paging):
    status: Literal["active", "inactive", "all", "lowstock"] = "active"


class _SearchInput(Paging):
    query: Query


class _IdInput(BaseModel):
    item_id: ZohoId


async def list_items(
    client: ZohoClient,
    status: str = "active",
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page[ItemSummary]:
    args = validated(_ListInput, status=status, page=page, per_page=per_page)
    params: dict[str, str | int] = {
        "filter_by": FILTER_BY[args.status],
        "page": args.page,
        "per_page": args.per_page,
    }
    body = await client.get("/items", params)
    found = [item_summary(r) for r in rows(body, "items")]
    next_page, warning = page_extras(client, body, args.page)
    return Page[ItemSummary](results=found, next_page=next_page, warning=warning)


async def get_item(client: ZohoClient, item_id: str) -> ItemDetail:
    args = validated(_IdInput, item_id=item_id)
    body = await client.get(f"/items/{args.item_id}", ttl=ITEM_DETAIL_TTL_S)
    return item_detail(record(body, "item"))


async def search_items(
    client: ZohoClient,
    query: str,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page[ItemSummary]:
    """Search by name or SKU (Zoho `search_text`, which also covers the description)."""
    args = validated(_SearchInput, query=query, page=page, per_page=per_page)
    params: dict[str, str | int] = {
        "search_text": args.query,
        "page": args.page,
        "per_page": args.per_page,
    }
    body = await client.get("/items", params)
    found = [item_summary(r) for r in rows(body, "items")]
    next_page, warning = page_extras(client, body, args.page)
    return Page[ItemSummary](results=found, next_page=next_page, warning=warning)
