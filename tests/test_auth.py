import asyncio
import logging
import socket
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import SecretStr

from zoho_connector.auth.oauth import (
    Organization,
    TokenManager,
    derive_api_domain,
    exchange_code,
    login,
    wait_for_callback,
)
from zoho_connector.auth.redact import RedactingFilter
from zoho_connector.auth.token_store import StoredTokens, TokenStore
from zoho_connector.config import Settings
from zoho_connector.errors import AuthRequiredError

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ORGS_URL = "https://www.zohoapis.com/inventory/v1/organizations"
SECRET = "s3cr3t-client-secret"
ACCESS = "1000.aaaabbbbccccddddeeeeffff00001111.22223333444455556666777788889999"
REFRESH = "1000.refresh0000000000000000000000000.refresh1111111111111111111111111"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        ZOHO_CLIENT_ID="cid",
        ZOHO_CLIENT_SECRET=SecretStr(SECRET),
        ZOHO_DC="com",
        TOKEN_ENCRYPTION_KEY=SecretStr(Fernet.generate_key().decode()),
    )


@pytest.fixture
def store(settings: Settings, tmp_path: Path) -> TokenStore:
    return TokenStore(settings.TOKEN_ENCRYPTION_KEY, tmp_path / "t.enc")


def _stored() -> StoredTokens:
    return StoredTokens(
        refresh_token=REFRESH,
        accounts_server="https://accounts.zoho.com",
        api_domain="https://www.zohoapis.com",
        org_id="42",
        org_name="Acme",
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------- token store


def test_store_round_trip(store: TokenStore) -> None:
    assert store.load() is None
    store.save(_stored())
    assert REFRESH.encode() not in store.path.read_bytes()
    loaded = store.load()
    assert loaded is not None and loaded.refresh_token == REFRESH and loaded.org_id == "42"
    store.clear()
    assert store.load() is None


def test_store_wrong_key(store: TokenStore) -> None:
    store.save(_stored())
    other = TokenStore(Fernet.generate_key().decode(), store.path)
    with pytest.raises(AuthRequiredError, match="TOKEN_ENCRYPTION_KEY"):
        other.load()


def test_store_missing_or_bad_key(tmp_path: Path) -> None:
    with pytest.raises(AuthRequiredError):
        TokenStore("", tmp_path / "t.enc")
    with pytest.raises(AuthRequiredError):
        TokenStore("not-a-fernet-key", tmp_path / "t.enc")


# ---------------------------------------------------------------- code exchange


@respx.mock
async def test_exchange_code_success(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).respond(
        json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}
    )
    async with httpx.AsyncClient() as http:
        tokens = await exchange_code(http, settings, "https://accounts.zoho.com", "the-code")
    assert tokens.refresh_token == REFRESH and tokens.access_token == ACCESS
    sent = route.calls.last.request.content.decode()
    assert "grant_type=authorization_code" in sent and "code=the-code" in sent


@respx.mock
async def test_exchange_code_missing_refresh_token(settings: Settings) -> None:
    respx.post("https://accounts.zoho.eu/oauth/v2/token").respond(
        json={"access_token": ACCESS, "expires_in": 3600}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(
            AuthRequiredError, match=r"Revoke the old grant at .*zoho\.eu.*Connected"
        ):
            await exchange_code(http, settings, "https://accounts.zoho.eu", "c")


@respx.mock
async def test_exchange_code_error_body(settings: Settings) -> None:
    respx.post(TOKEN_URL).respond(json={"error": "invalid_code"})
    async with httpx.AsyncClient() as http:
        with pytest.raises(AuthRequiredError, match="invalid_code"):
            await exchange_code(http, settings, "https://accounts.zoho.com", "c")


# ---------------------------------------------------------------- callback / state


def test_derive_api_domain() -> None:
    assert derive_api_domain("https://accounts.zoho.eu") == "https://www.zohoapis.eu"
    assert derive_api_domain("https://accounts.zoho.com.au/") == "https://www.zohoapis.com.au"
    with pytest.raises(AuthRequiredError):
        derive_api_domain("https://accounts.zoho.evil.example")


def test_state_mismatch_rejected() -> None:
    port = _free_port()
    uri = f"http://127.0.0.1:{port}/callback"

    async def run() -> None:
        task = asyncio.create_task(asyncio.to_thread(wait_for_callback, uri, "good-state", 10))
        await asyncio.sleep(0.3)

        def hit() -> int:
            try:
                urllib.request.urlopen(f"{uri}?code=abc&state=evil", timeout=5)
            except urllib.error.HTTPError as exc:
                return exc.code
            return 200

        assert await asyncio.to_thread(hit) == 400
        with pytest.raises(AuthRequiredError, match="state mismatch"):
            await task

    asyncio.run(run())


@respx.mock
async def test_login_full_flow_stores_org(settings: Settings, store: TokenStore) -> None:
    port = _free_port()
    settings.ZOHO_REDIRECT_URI = f"http://127.0.0.1:{port}/callback"
    respx.post(TOKEN_URL).respond(
        json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}
    )
    respx.get(ORGS_URL).respond(json={"organizations": [{"organization_id": 42, "name": "Acme"}]})

    def fake_browser(url: str) -> None:
        state = url.split("state=")[1].split("&")[0]
        callback = f"http://127.0.0.1:{port}/callback?code=abc&state={state}"
        import threading

        threading.Thread(
            target=lambda: urllib.request.urlopen(callback, timeout=5).read(), daemon=True
        ).start()

    stored = await login(
        settings, store, open_browser=fake_browser, echo=lambda _: None, timeout=10
    )
    assert stored.org_id == "42" and store.load() is not None


# ---------------------------------------------------------------- token manager


@respx.mock
async def test_refresh_success_and_cache(settings: Settings, store: TokenStore) -> None:
    store.save(_stored())
    route = respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "expires_in": 3600})
    mgr = TokenManager(settings, store)
    assert await mgr.get_access_token() == ACCESS
    assert await mgr.get_access_token() == ACCESS
    assert route.call_count == 1
    assert 3500 < mgr.seconds_left() <= 3600
    mgr.invalidate()
    await mgr.get_access_token()
    assert route.call_count == 2


@respx.mock
async def test_refresh_when_nearly_expired(settings: Settings, store: TokenStore) -> None:
    store.save(_stored())
    now = [1000.0]
    route = respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "expires_in": 3600})
    mgr = TokenManager(settings, store, clock=lambda: now[0])
    await mgr.get_access_token()
    now[0] += 3600 - 200  # under the 300 s margin
    await mgr.get_access_token()
    assert route.call_count == 2


@respx.mock
async def test_refresh_failure_raises_auth_required(settings: Settings, store: TokenStore) -> None:
    store.save(_stored())
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_code"})
    with pytest.raises(AuthRequiredError):
        await TokenManager(settings, store).get_access_token()


@respx.mock
async def test_concurrent_callers_refresh_once(settings: Settings, store: TokenStore) -> None:
    store.save(_stored())
    route = respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "expires_in": 3600})
    mgr = TokenManager(settings, store)
    results = await asyncio.gather(*(mgr.get_access_token() for _ in range(10)))
    assert results == [ACCESS] * 10
    assert route.call_count == 1


# ---------------------------------------------------------------- redaction


def test_redaction_filter_hides_tokens_and_secret() -> None:
    flt = RedactingFilter(SECRET)
    record = logging.LogRecord(
        "t", logging.WARNING, __file__, 1, "token %s secret %s", (ACCESS, SECRET), None
    )
    assert flt.filter(record)
    out = record.getMessage()
    assert ACCESS not in out and SECRET not in out and "[REDACTED]" in out

    plain = logging.LogRecord("t", logging.INFO, __file__, 1, f"refresh={REFRESH}", None, None)
    flt.filter(plain)
    assert REFRESH not in plain.getMessage()


def test_organization_dataclass() -> None:
    assert Organization("1", "x").name == "x"
