"""Log safety: a full login, a refresh and five tool calls with DEBUG capture.

No redaction filter is installed here on purpose: the test proves the code never *emits* a
token or the client secret, rather than relying on the filter to clean it up afterwards.
"""

import asyncio
import json
import logging
import re
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import SecretStr

from zoho_connector import tools
from zoho_connector.auth.oauth import TokenManager, login
from zoho_connector.auth.token_store import TokenStore
from zoho_connector.client.limits import DailyBudget
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
API = "https://www.zohoapis.com/inventory/v1"
CLIENT_SECRET = "cs-0123456789abcdef-client-secret"
LOGIN_ACCESS = "1000.loginaccess0000000000000000000000.loginaccess1111111111111111111111"
REFRESH = "1000.refreshtoken00000000000000000000.refreshtoken111111111111111111111"
REFRESHED_ACCESS = "1000.refreshedaccess00000000000000000.refreshedaccess2222222222222222"
GRANT_CODE = "1000.grantcode00000000000000000000000.grantcode3333333333333333333"
SENSITIVE = (CLIENT_SECRET, LOGIN_ACCESS, REFRESH, REFRESHED_ACCESS, GRANT_CODE)
ORDER_ID = "4261357000000039414"
ITEM_ID = "4261357000000039207"

TOKEN_PREFIX = re.compile(r"(?<![0-9])1000\.")  # any string starting with "1000."


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def everything_logged(records: list[logging.LogRecord]) -> list[str]:
    """Every string a record could put on screen: message, raw args, extras, traceback."""
    chunks: list[str] = []
    for r in records:
        chunks.append(r.getMessage())
        chunks.append(str(r.msg))
        chunks.append(repr(r.args))
        chunks.append(r.exc_text or "")
        if r.exc_info and r.exc_info[1] is not None:
            chunks.append(repr(r.exc_info[1]))
        chunks.extend(repr(v) for k, v in r.__dict__.items() if k not in {"args", "msg"})
    return chunks


def find_leaks(records: list[logging.LogRecord]) -> list[str]:
    leaks = []
    for chunk in everything_logged(records):
        if TOKEN_PREFIX.search(chunk) or any(secret in chunk for secret in SENSITIVE):
            leaks.append(chunk[:120])
    return leaks


def test_the_detector_catches_leaks() -> None:
    """Negative control: without this, an always-green detector would pass silently."""

    def rec(msg: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args or None, None)

    assert find_leaks([rec("clean line ms=1000 used=21000.5")]) == []
    assert find_leaks([rec("token %s", LOGIN_ACCESS)])
    assert find_leaks([rec("secret " + CLIENT_SECRET)])
    assert find_leaks([rec("code=%s", GRANT_CODE)])


@respx.mock
async def test_login_refresh_and_five_tool_calls_log_no_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        _env_file=None,
        ZOHO_CLIENT_ID="cid",
        ZOHO_CLIENT_SECRET=SecretStr(CLIENT_SECRET),
        ZOHO_REDIRECT_URI="http://127.0.0.1:0/callback",  # port 0: the OS picks a free one
        TOKEN_ENCRYPTION_KEY=SecretStr(Fernet.generate_key().decode()),
    )
    store = TokenStore(settings.TOKEN_ENCRYPTION_KEY, tmp_path / "tokens.enc")

    # accounts.zoho.com: first the code exchange (login), then the refresh.
    token_route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"access_token": LOGIN_ACCESS, "refresh_token": REFRESH, "expires_in": 3600},
            ),
            httpx.Response(200, json={"access_token": REFRESHED_ACCESS, "expires_in": 3600}),
        ]
    )
    respx.get(f"{API}/organizations").respond(
        json={"organizations": [{"organization_id": 42, "name": "Acme"}]}
    )
    # The first data call is rate limited once, so the retry path is logged too.
    orders = respx.get(f"{API}/salesorders").mock(
        side_effect=[
            httpx.Response(429, json={"code": 44, "message": "Too many requests"}),
            httpx.Response(200, json=fixture("salesorders_list")),
            httpx.Response(200, json=fixture("salesorders_search")),
        ]
    )
    respx.get(f"{API}/salesorders/{ORDER_ID}").respond(json=fixture("salesorder_detail"))
    respx.get(f"{API}/items").respond(json=fixture("items_list"))
    respx.get(f"{API}/items/{ITEM_ID}").respond(json=fixture("item_detail"))

    def fake_browser(url: str) -> None:
        state = parse_qs(urlparse(url).query)["state"][0]
        redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
        callback = f"{redirect}?code={GRANT_CODE}&state={state}"
        threading.Thread(
            target=lambda: urllib.request.urlopen(callback, timeout=5).read(), daemon=True
        ).start()

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    echoed: list[str] = []
    with caplog.at_level(logging.DEBUG):  # root logger: our code, httpx, anyio, everything
        stored = await login(
            settings, store, open_browser=fake_browser, echo=echoed.append, timeout=10
        )
        assert stored.org_id == "42"

        async with ZohoClient(
            settings,
            store,
            TokenManager(settings, store),
            budget=DailyBudget(900, path=None),
            sleep=no_sleep,
        ) as client:
            await tools.list_sales_orders(client)
            await tools.get_sales_order(client, ORDER_ID)
            await tools.search_sales_orders(client, "alice")
            await tools.list_items(client)
            await tools.get_item(client, ITEM_ID)

    # The run really did what it claims to have done ...
    assert token_route.call_count == 2  # one code exchange, one refresh
    assert orders.call_count == 3  # 429, then the list, then the search
    sent = [
        c.request.headers.get("Authorization")
        for c in respx.calls
        if "/inventory/" in str(c.request.url)
    ]
    assert f"Zoho-oauthtoken {REFRESHED_ACCESS}" in sent  # the refreshed token was used on the wire
    # ... the secrets really were in play ...
    assert CLIENT_SECRET in token_route.calls.last.request.content.decode()
    # ... and the capture is not vacuous.
    ours = [r for r in caplog.records if r.name.startswith("zoho_connector")]
    assert len(ours) >= 6
    assert any("status=429" in r.getMessage() for r in ours)
    assert any("attempt=2" in r.getMessage() for r in ours)

    assert find_leaks(caplog.records) == []
    # What `auth login` prints to the terminal must not hold secrets either.
    assert not any(TOKEN_PREFIX.search(line) or CLIENT_SECRET in line for line in echoed)


@respx.mock
async def test_failure_paths_log_no_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rejected token, exhausted 5xx and a failed refresh: messages and tracebacks stay clean."""
    settings = Settings(
        _env_file=None,
        ZOHO_CLIENT_ID="cid",
        ZOHO_CLIENT_SECRET=SecretStr(CLIENT_SECRET),
        TOKEN_ENCRYPTION_KEY=SecretStr(Fernet.generate_key().decode()),
    )
    store = TokenStore(settings.TOKEN_ENCRYPTION_KEY, tmp_path / "tokens.enc")
    from zoho_connector.auth.token_store import StoredTokens
    from zoho_connector.errors import ConnectorError

    store.save(
        StoredTokens(
            refresh_token=REFRESH,
            accounts_server="https://accounts.zoho.com",
            api_domain="https://www.zohoapis.com",
            org_id="42",
            org_name="Acme",
        )
    )
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": LOGIN_ACCESS, "expires_in": 3600}),
            httpx.Response(400, json={"error": "invalid_code", "echo": REFRESH}),
        ]
    )
    respx.get(f"{API}/items").mock(
        side_effect=[httpx.Response(401, json={"message": f"bad token {LOGIN_ACCESS}"})] * 2
    )

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    errors: list[ConnectorError] = []
    with caplog.at_level(logging.DEBUG):
        async with ZohoClient(
            settings,
            store,
            TokenManager(settings, store),
            budget=DailyBudget(900, path=None),
            sleep=no_sleep,
        ) as client:
            try:
                await tools.list_items(client)
            except ConnectorError as exc:
                errors.append(exc)
    assert errors, "the 401 -> failed refresh path should have raised"
    assert find_leaks(caplog.records) == []
    assert not any(secret in errors[0].message for secret in SENSITIVE)


@respx.mock
async def test_revoke_never_logs_the_refresh_token_even_with_no_cli_logging_setup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zoho's revoke endpoint takes the refresh token as a query parameter, and httpx logs
    request URLs at INFO. No handler filter and no WARNING level here: the library itself
    must keep the token out of the records."""
    from zoho_connector.auth.oauth import revoke_refresh_token
    from zoho_connector.auth.token_store import StoredTokens

    route = respx.post("https://accounts.zoho.com/oauth/v2/token/revoke").respond(
        json={"status": "success"}
    )
    stored = StoredTokens(
        refresh_token=REFRESH,
        accounts_server="https://accounts.zoho.com",
        api_domain="https://www.zohoapis.com",
        org_id="42",
        org_name="Acme",
    )
    with caplog.at_level(logging.DEBUG):
        await revoke_refresh_token(stored)
    # Zoho's required request format is unchanged: the token still goes out as ?token=...
    assert route.calls.last.request.url.params["token"] == REFRESH
    assert any(r.name == "httpx" for r in caplog.records), "httpx should have logged the request"
    assert find_leaks(caplog.records) == []
    assert REFRESH not in caplog.text
    assert "token=[REDACTED]" in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        'HTTP Request: POST https://a.zoho.com/revoke?token=abc123 "HTTP/1.1 200 OK"',
        "https://a.zoho.com/revoke?x=1&token=abc123&y=2",
    ],
)
def test_redact_scrubs_token_query_params_of_any_shape(line: str) -> None:
    from zoho_connector.auth.redact import redact

    out = redact(line)
    assert "abc123" not in out and "token=[REDACTED]" in out
    assert "x=1" in out or "revoke" in out  # the rest of the line survives
