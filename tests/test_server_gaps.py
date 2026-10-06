"""Server tests that close gaps found in the audit: every error code's JSON shape, the HTTP
bearer wiring, demo mode with the network switched off, and the committed tools.json."""

import json
import socket
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
from mcp import Client

from zoho_connector import server
from zoho_connector.config import Settings
from zoho_connector.errors import (
    AuthRequiredError,
    ConnectorError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
)
from zoho_connector.server import (
    BearerAuthMiddleware,
    create_http_app,
    create_server,
    error_payload,
    export_tool_definitions,
)

ROOT = Path(__file__).parent.parent
ACCESS = "1000.aaaabbbbccccddddeeeeffff00001111.22223333444455556666777788889999"
ERROR_KEYS = {"code", "message", "retryable", "agent_action"}

# code -> (exception, retryable, agent_action) exactly as the agent should see them
EXPECTED = {
    "NOT_FOUND": (NotFoundError, False, "Ask the user to check the ID or use a search tool."),
    "INVALID_INPUT": (
        InvalidInputError,
        False,
        "Fix the arguments as the message says, then call again.",
    ),
    "RATE_LIMITED": (RateLimitedError, True, "Wait about a minute, then retry once."),
    "DAILY_QUOTA_EXHAUSTED": (
        DailyQuotaExhaustedError,
        False,
        "Stop. Tell the user the daily limit is used.",
    ),
    "AUTH_REQUIRED": (
        AuthRequiredError,
        False,
        "Stop. Tell the user an admin must run auth login.",
    ),
    "UPSTREAM_ERROR": (
        UpstreamError,
        True,
        "Retry once later. If it fails again, tell the user Zoho is failing.",
    ),
}


def demo_settings(**extra: Any) -> Settings:
    return Settings(_env_file=None, ZOHO_DEMO=True, DAILY_BUDGET=900, **extra)


# ------------------------------------------------------------------ error JSON shape


@pytest.mark.parametrize("code", sorted(EXPECTED))
def test_error_payload_for_every_code(code: str) -> None:
    exc_type, retryable, action = EXPECTED[code]
    payload = error_payload(exc_type("something happened"))
    assert set(payload) == ERROR_KEYS
    assert payload == {
        "code": code,
        "message": "something happened",
        "retryable": retryable,
        "agent_action": action,
    }
    assert json.loads(json.dumps(payload)) == payload


def test_error_payload_redacts_tokens_and_truncates() -> None:
    payload = error_payload(UpstreamError(f"token {ACCESS} was rejected " + "x" * 1000))
    assert ACCESS not in str(payload["message"]) and "1000." not in str(payload["message"])
    assert len(str(payload["message"])) <= 300


def test_error_payload_for_an_unknown_subclass_uses_the_upstream_row() -> None:
    class Odd(ConnectorError):
        code = "SOMETHING_NEW"

    payload = error_payload(Odd("hm"))
    assert payload["code"] == "SOMETHING_NEW" and payload["retryable"] is True


class RaisingClient:
    """Stands in for ZohoClient inside the server: every Zoho call raises `exc`."""

    def __init__(self, exc: ConnectorError) -> None:
        self.exc = exc

    async def __aenter__(self) -> "RaisingClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def get(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise self.exc

    def quota(self) -> dict[str, Any]:
        return {"used_today": 0, "budget": 900, "remaining": 900, "warning": False}


@pytest.mark.parametrize("code", sorted(EXPECTED))
async def test_every_error_code_reaches_the_agent_as_json(
    code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc_type, retryable, action = EXPECTED[code]
    monkeypatch.setattr(
        server, "build_client", lambda settings: RaisingClient(exc_type(f"bad {ACCESS}"))
    )
    async with Client(create_server(demo_settings())) as client:
        result = await client.call_tool("list_items", {})
    assert result.is_error
    assert len(result.content) == 1
    body = json.loads(result.content[0].text)  # plain JSON, no "Error executing tool" prefix
    assert set(body) == ERROR_KEYS
    assert (body["code"], body["retryable"], body["agent_action"]) == (code, retryable, action)
    assert ACCESS not in result.content[0].text
    assert "Traceback" not in result.content[0].text


async def test_invalid_arguments_in_every_tool_are_json_errors() -> None:
    bad_calls = {
        "list_sales_orders": {"date_from": "not-a-date"},
        "get_sales_order": {"salesorder_id": "../x"},
        "search_sales_orders": {"query": "a"},
        "list_items": {"per_page": 0},
        "get_item": {"item_id": ""},
        "search_items": {"query": " "},
    }
    async with Client(create_server(demo_settings())) as client:
        for name, args in bad_calls.items():
            result = await client.call_tool(name, args)
            assert result.is_error, name
            body = json.loads(result.content[0].text)
            assert set(body) == ERROR_KEYS and body["code"] == "INVALID_INPUT", name


# ------------------------------------------------------------------ HTTP bearer wiring


def _has_bearer(app: Any) -> bool:
    return any(m.cls is BearerAuthMiddleware for m in app.user_middleware)


def test_bearer_middleware_is_installed_only_when_a_token_is_configured() -> None:
    assert _has_bearer(create_http_app(demo_settings(MCP_SERVER_TOKEN="s3cret")))
    assert not _has_bearer(create_http_app(demo_settings()))


async def test_bearer_check_ignores_case_of_header_name_but_not_the_token() -> None:
    called: list[bool] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        called.append(True)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guarded = BearerAuthMiddleware(inner, "s3cret")
    transport = httpx.ASGITransport(app=guarded)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        assert (await http.get("/", headers={"authorization": "Bearer s3cret"})).status_code == 204
        for bad in ("Bearer s3cret ", "Bearer S3CRET", "Bearer", "Basic s3cret", "s3cret", ""):
            resp = await http.get("/", headers={"Authorization": bad})
            assert resp.status_code == 401, bad
            assert resp.headers["www-authenticate"] == "Bearer"
            assert resp.json() == {"error": "unauthorized"}
    assert called == [True]  # the guarded app ran for the one good request only


async def test_bearer_middleware_does_not_leak_the_token_in_the_401() -> None:
    app = create_http_app(demo_settings(MCP_SERVER_TOKEN="s3cret"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        resp = await http.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401 and "s3cret" not in resp.text


# ------------------------------------------------------------------ demo mode: zero network


async def test_demo_mode_over_mcp_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("demo mode tried to use the network")

    async def forbidden_async(*_: Any, **__: Any) -> Any:
        forbidden()

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)

    calls: dict[str, dict[str, Any]] = {
        "list_sales_orders": {},
        "get_sales_order": {"salesorder_id": "4261357000000039414"},
        "search_sales_orders": {"query": "alice"},
        "list_items": {},
        "get_item": {"item_id": "4261357000000039207"},
        "search_items": {"query": "mug"},
        "connector_status": {},
    }
    async with Client(create_server(demo_settings())) as client:
        for name, args in calls.items():
            result = await client.call_tool(name, args)
            assert not result.is_error, (name, result.content)
        status = (await client.call_tool("connector_status", {})).structured_content
    assert status["mode"] == "demo" and status["used_today"] == 0


# ------------------------------------------------------------------ tool surface


async def test_tool_names_describe_reads_only() -> None:
    listed = await create_server(demo_settings()).list_tools()
    for tool in listed:
        assert tool.name.split("_")[0] in {"get", "list", "search", "connector"}, tool.name
        assert not {"create", "update", "delete", "cancel", "void", "post"} & set(
            tool.name.split("_")
        )


async def test_committed_tools_json_matches_the_registered_tools() -> None:
    """tools.json is what gets pasted into Agent Studio; it must not drift from the server."""
    committed = json.loads((ROOT / "tools.json").read_text(encoding="utf-8"))
    live = json.loads(json.dumps(await export_tool_definitions(demo_settings())))
    assert committed == live, "tools.json is stale: run `zoho-connector export-tools`"
