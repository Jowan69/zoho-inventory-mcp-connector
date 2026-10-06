"""MCP server tests: in-memory client session against the server in demo mode (no network)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from typer.testing import CliRunner

from zoho_connector.cli import app
from zoho_connector.config import Settings
from zoho_connector.server import BearerAuthMiddleware, create_http_app, create_server

ORDER_ID = "4261357000000039414"
ITEM_ID = "4261357000000039207"
TOOL_NAMES = {
    "list_sales_orders",
    "get_sales_order",
    "search_sales_orders",
    "list_items",
    "get_item",
    "search_items",
    "connector_status",
}
DEMO_CALLS: dict[str, dict[str, Any]] = {
    "list_sales_orders": {},
    "get_sales_order": {"salesorder_id": ORDER_ID},
    "search_sales_orders": {"query": "SO-0001"},
    "list_items": {},
    "get_item": {"item_id": ITEM_ID},
    "search_items": {"query": "notebook"},
    "connector_status": {},
}


def demo_settings(**extra: Any) -> Settings:
    return Settings(_env_file=None, ZOHO_DEMO=True, DAILY_BUDGET=900, **extra)


def session() -> Client:
    """In-memory MCP client session; open it inside the test (anyio scopes are task-bound)."""
    return Client(create_server(demo_settings()))


async def test_lists_seven_tools() -> None:
    async with session() as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOL_NAMES


async def test_every_tool_is_read_only_with_title_and_short_description() -> None:
    async with session() as client:
        for tool in (await client.list_tools()).tools:
            ann = tool.annotations
            assert ann is not None, tool.name
            assert ann.read_only_hint is True
            assert ann.idempotent_hint is True
            assert ann.destructive_hint is False
            assert ann.open_world_hint is True
            assert tool.title and ann.title == tool.title
            desc = tool.description or ""
            assert desc.split()[0] in {"Get", "List", "Search", "Report"}, tool.name
            assert desc.endswith("Read only."), tool.name
            assert len(desc.split()) < 60, tool.name
            for name, prop in tool.input_schema.get("properties", {}).items():
                assert prop.get("description"), f"{tool.name}.{name} has no description"


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
async def test_each_tool_returns_valid_result_on_demo_data(name: str) -> None:
    async with session() as client:
        result = await client.call_tool(name, DEMO_CALLS[name])
        assert not result.is_error, result.content
        assert result.structured_content
        assert json.loads(result.content[0].text) == result.structured_content


async def test_search_returns_demo_orders() -> None:
    async with session() as client:
        result = await client.call_tool("search_sales_orders", {"query": "SO-0001"})
        assert result.structured_content["results"]


async def test_not_found_returns_json_error_shape() -> None:
    async with session() as client:
        result = await client.call_tool("get_sales_order", {"salesorder_id": "1"})
        assert result.is_error
        body = json.loads(result.content[0].text)
        assert set(body) == {"code", "message", "retryable", "agent_action"}
        assert body["code"] == "NOT_FOUND"
        assert body["retryable"] is False
        assert body["agent_action"] == "Ask the user to check the ID or use a search tool."


async def test_invalid_input_returns_json_error_shape() -> None:
    async with session() as client:
        result = await client.call_tool("get_item", {"item_id": "abc"})
        assert result.is_error
        body = json.loads(result.content[0].text)
        assert body["code"] == "INVALID_INPUT"
        assert "Traceback" not in body["message"]


# HTTP transport -----------------------------------------------------------------------------


async def _post(app: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.post("/mcp", json={}, headers=headers or {})


async def test_http_rejects_missing_bearer_token() -> None:
    app_ = create_http_app(demo_settings(MCP_SERVER_TOKEN="s3cret"))
    assert (await _post(app_)).status_code == 401
    assert (await _post(app_, {"Authorization": "Bearer wrong"})).status_code == 401
    assert (await _post(app_, {"Authorization": "s3cret"})).status_code == 401


async def test_bearer_middleware_passes_correct_token() -> None:
    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guarded = BearerAuthMiddleware(inner, "s3cret")
    assert (await _post(guarded, {"Authorization": "Bearer s3cret"})).status_code == 204


# export-tools -------------------------------------------------------------------------------


def test_export_tools_writes_registered_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZOHO_DEMO", "1")
    out = tmp_path / "tools.json"
    result = CliRunner().invoke(app, ["export-tools", "--output", str(out)])
    assert result.exit_code == 0, result.output
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert {t["name"] for t in exported} == TOOL_NAMES
    for t in exported:
        assert set(t) == {"name", "title", "description", "inputSchema", "annotations"}
        assert t["annotations"]["readOnlyHint"] is True
