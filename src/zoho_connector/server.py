"""MCP server entry point: registers the read-only tools and runs the server."""

import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from zoho_connector import tools
from zoho_connector.auth.oauth import TokenManager
from zoho_connector.auth.redact import redact
from zoho_connector.auth.token_store import TokenStore
from zoho_connector.client.demo import build_demo_client
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import ConnectorError
from zoho_connector.models import (
    ConnectorStatus,
    ItemDetail,
    ItemSummary,
    OrderDetail,
    OrderSummary,
    Page,
)

SERVER_NAME = "zoho-inventory-readonly"
MAX_MESSAGE_CHARS = 300

# Per error code: (retryable, what the agent should do next). Unlisted codes use the last row.
_AGENT_ACTIONS: dict[str, tuple[bool, str]] = {
    "AUTH_REQUIRED": (False, "Stop. Tell the user an admin must run auth login."),
    "DAILY_QUOTA_EXHAUSTED": (False, "Stop. Tell the user the daily limit is used."),
    "NOT_FOUND": (False, "Ask the user to check the ID or use a search tool."),
    "INVALID_INPUT": (False, "Fix the arguments as the message says, then call again."),
    "RATE_LIMITED": (True, "Wait about a minute, then retry once."),
    "UPSTREAM_ERROR": (True, "Retry once later. If it fails again, tell the user Zoho is failing."),
}


@dataclass
class AppState:
    """What the lifespan shares with every tool call: one client, so limits are global."""

    client: ZohoClient
    settings: Settings


def build_client(settings: Settings) -> ZohoClient:
    if settings.ZOHO_DEMO:
        return build_demo_client(settings)
    store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
    return ZohoClient(settings, store, TokenManager(settings, store))


def error_payload(exc: ConnectorError) -> dict[str, object]:
    """The JSON the agent sees. Only the sanitized message; never tokens, traces or bodies."""
    retryable, action = _AGENT_ACTIONS.get(exc.code, _AGENT_ACTIONS["UPSTREAM_ERROR"])
    message = redact(exc.message)[:MAX_MESSAGE_CHARS]
    return {"code": exc.code, "message": message, "retryable": retryable, "agent_action": action}


def _error_result(exc: ConnectorError) -> CallToolResult:
    # Returned rather than raised as ToolError: the SDK prefixes a raised ToolError's text with
    # "Error executing tool ...", which would stop the text from being plain JSON.
    text = json.dumps(error_payload(exc))
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _ok_result(model: BaseModel) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=model.model_dump_json())],
        structured_content=model.model_dump(mode="json"),
    )


async def _run(
    ctx: Context[AppState], call: Callable[[ZohoClient, Settings], Awaitable[BaseModel]]
) -> CallToolResult:
    state: AppState = ctx.request_context.lifespan_context
    try:
        return _ok_result(await call(state.client, state.settings))
    except ConnectorError as exc:
        return _error_result(exc)


_PAGE = Annotated[
    int, Field(description="Page number, starting at 1. Use next_page from the previous result.")
]
_PER_PAGE = Annotated[int, Field(description="Results per page, 1 to 100. Default 25.")]
_STATUS = Annotated[
    str | None,
    Field(description="Order status, lowercase: draft, confirmed, fulfilled, void. Omit for all."),
]


def create_server(settings: Settings | None = None) -> MCPServer[AppState]:
    """Build the MCP server and register the seven read-only tools."""
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(server: MCPServer[AppState]) -> AsyncIterator[AppState]:
        async with build_client(cfg) as client:
            yield AppState(client=client, settings=cfg)

    mcp = MCPServer(SERVER_NAME, lifespan=lifespan)

    def register(name: str, title: str, description: str) -> Callable[[Any], Any]:
        return mcp.tool(
            name=name,
            title=title,
            description=description,
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=True,
                idempotent_hint=True,
                destructive_hint=False,
                open_world_hint=True,
            ),
        )

    @register(
        "list_sales_orders",
        "List sales orders",
        "List sales orders, optionally filtered by status and date range. Use to browse orders in "
        "a period; use search_sales_orders when you have an order number or customer name. Dates "
        "are ISO YYYY-MM-DD, inclusive. Status is lowercase, e.g. draft, confirmed, fulfilled, "
        "void. Follow next_page for more. Read only.",
    )
    async def list_sales_orders(
        ctx: Context[AppState],
        status: _STATUS = None,
        date_from: Annotated[
            str | None, Field(description="Earliest order date, ISO YYYY-MM-DD, inclusive.")
        ] = None,
        date_to: Annotated[
            str | None, Field(description="Latest order date, ISO YYYY-MM-DD, inclusive.")
        ] = None,
        page: _PAGE = 1,
        per_page: _PER_PAGE = 25,
    ) -> Annotated[CallToolResult, Page[OrderSummary]]:
        return await _run(
            ctx,
            lambda c, _s: tools.list_sales_orders(c, status, date_from, date_to, page, per_page),
        )

    @register(
        "get_sales_order",
        "Get a sales order",
        "Get one sales order with its line items. Use only when you have salesorder_id (digits "
        "only, from a list or search result); use search_sales_orders when you have an order "
        "number or customer name. Contact details are masked. Read only.",
    )
    async def get_sales_order(
        ctx: Context[AppState],
        salesorder_id: Annotated[
            str, Field(description="Zoho sales order ID, digits only, e.g. 4261357000000039414.")
        ],
    ) -> Annotated[CallToolResult, OrderDetail]:
        return await _run(ctx, lambda c, _s: tools.get_sales_order(c, salesorder_id))

    @register(
        "search_sales_orders",
        "Search sales orders",
        "Search sales orders by order number, reference or customer name. Use when you have any "
        "of those; use get_sales_order only when you have salesorder_id, and list_sales_orders to "
        "browse by date. Query needs 2+ characters. Status is lowercase, e.g. confirmed. Follow "
        "next_page for more. Read only.",
    )
    async def search_sales_orders(
        ctx: Context[AppState],
        query: Annotated[
            str,
            Field(description="Order number like SO-00011, reference number or customer name."),
        ],
        status: _STATUS = None,
        page: _PAGE = 1,
        per_page: _PER_PAGE = 25,
    ) -> Annotated[CallToolResult, Page[OrderSummary]]:
        return await _run(
            ctx, lambda c, _s: tools.search_sales_orders(c, query, status, page, per_page)
        )

    @register(
        "list_items",
        "List items",
        "List inventory items by status. Use to browse the catalog or find low stock; use "
        "search_items when you have a name or SKU, and get_item for stock per location. Status is "
        "active (default), inactive, all or lowstock. Follow next_page for more. Read only.",
    )
    async def list_items(
        ctx: Context[AppState],
        status: Annotated[
            Literal["active", "inactive", "all", "lowstock"],
            Field(description="One of active, inactive, all, lowstock. Default active."),
        ] = "active",
        page: _PAGE = 1,
        per_page: _PER_PAGE = 25,
    ) -> Annotated[CallToolResult, Page[ItemSummary]]:
        return await _run(ctx, lambda c, _s: tools.list_items(c, status, page, per_page))

    @register(
        "get_item",
        "Get an item",
        "Get one item with stock per location. Use only when you have item_id (digits only, from "
        "a list or search result); use search_items when you have a name or SKU. Read only.",
    )
    async def get_item(
        ctx: Context[AppState],
        item_id: Annotated[
            str, Field(description="Zoho item ID, digits only, e.g. 4261357000000039207.")
        ],
    ) -> Annotated[CallToolResult, ItemDetail]:
        return await _run(ctx, lambda c, _s: tools.get_item(c, item_id))

    @register(
        "search_items",
        "Search items",
        "Search items by name or SKU. Use when you have part of a name or SKU; use get_item only "
        "when you have item_id, and list_items to browse by status. Query needs 2+ characters. "
        "Follow next_page for more. Read only.",
    )
    async def search_items(
        ctx: Context[AppState],
        query: Annotated[str, Field(description="Part of an item name or SKU, 2+ characters.")],
        page: _PAGE = 1,
        per_page: _PER_PAGE = 25,
    ) -> Annotated[CallToolResult, Page[ItemSummary]]:
        return await _run(ctx, lambda c, _s: tools.search_items(c, query, page, per_page))

    @register(
        "connector_status",
        "Connector status",
        "Report connector mode (live or demo), login state, organization and today's request "
        "usage against the daily budget. Use before large lookups or after a tool error; it makes "
        "no Zoho call. Read only.",
    )
    async def connector_status(
        ctx: Context[AppState],
    ) -> Annotated[CallToolResult, ConnectorStatus]:
        return await _run(ctx, tools.connector_status)

    return mcp


class BearerAuthMiddleware:
    """Pure ASGI middleware: 401 unless the request carries `Authorization: Bearer <token>`."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = b""
        for key, value in scope["headers"]:
            if key == b"authorization":
                supplied = value
                break
        if not hmac.compare_digest(supplied, self._expected):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def create_http_app(settings: Settings | None = None, host: str = "127.0.0.1") -> ASGIApp:
    """Streamable HTTP app at /mcp, behind the bearer check when MCP_SERVER_TOKEN is set."""
    cfg = settings if settings is not None else Settings()
    app = create_server(cfg).streamable_http_app(host=host)
    token = cfg.MCP_SERVER_TOKEN.get_secret_value()
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token)
    return app


async def export_tool_definitions(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Tool metadata read from the live registration (what `tools/list` would return)."""
    listed = await create_server(settings).list_tools()
    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "inputSchema": t.input_schema,
            "annotations": (
                t.annotations.model_dump(by_alias=True, exclude_none=True)
                if t.annotations
                else None
            ),
        }
        for t in listed
    ]
