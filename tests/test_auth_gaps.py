"""Auth tests that close gaps found in the audit: scopes, callback checks, login branches,
the refresh request itself, the token store's failure paths and revoke."""

import socket
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from pydantic import SecretStr

from zoho_connector.auth.oauth import (
    SCOPES,
    Organization,
    TokenManager,
    build_auth_url,
    check_callback,
    login,
    revoke_refresh_token,
)
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


def _redirect_of(auth_url: str) -> str:
    """The redirect_uri the login put in the authorization URL (holds the real bound port)."""
    return parse_qs(urlparse(auth_url).query)["redirect_uri"][0]


def _browser(extra: str = "") -> Callable[[str], None]:
    """A fake browser: reads `state` from the auth URL and hits the callback like Zoho would."""

    def open_url(url: str) -> None:
        state = parse_qs(urlparse(url).query)["state"][0]
        callback = f"{_redirect_of(url)}?code=abc&state={state}{extra}"
        threading.Thread(
            target=lambda: _hit(callback),
            daemon=True,
        ).start()

    return open_url


def _hit(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except OSError:
        pass  # a rejected callback answers 400; the test asserts on login()'s exception


# ---------------------------------------------------------------- authorization URL


def test_auth_url_requests_read_scopes_and_offline_access(settings: Settings) -> None:
    query = parse_qs(urlparse(build_auth_url(settings, "st4te")).query)
    assert query["scope"] == [
        "ZohoInventory.items.READ,ZohoInventory.salesorders.READ,ZohoInventory.settings.READ"
    ]
    assert SCOPES == query["scope"][0]
    assert query["access_type"] == ["offline"]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["st4te"]
    assert query["client_id"] == ["cid"]
    assert SECRET not in build_auth_url(settings, "st4te")


def test_scopes_are_all_read_only() -> None:
    assert SCOPES and all(s.endswith(".READ") for s in SCOPES.split(","))


# ---------------------------------------------------------------- state / callback checks


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"code": "abc"}, "state mismatch"),  # state missing entirely
        ({"code": "abc", "state": ""}, "state mismatch"),
        ({"code": "abc", "state": "other"}, "state mismatch"),
        ({"state": "good", "error": "access_denied"}, "access_denied"),
        ({"state": "good"}, "authorization code"),
    ],
)
def test_check_callback_rejects(params: dict[str, str], message: str) -> None:
    with pytest.raises(AuthRequiredError, match=message):
        check_callback(params, "good")


def test_check_callback_accepts_matching_state() -> None:
    params = {"code": "abc", "state": "good"}
    assert check_callback(params, "good") == params


# ---------------------------------------------------------------- login branches


async def test_login_needs_credentials_and_opens_no_browser(store: TokenStore) -> None:
    opened: list[str] = []
    with pytest.raises(AuthRequiredError, match="ZOHO_CLIENT_ID"):
        await login(
            Settings(_env_file=None),
            store,
            open_browser=opened.append,
            echo=lambda _: None,
            timeout=1,
        )
    assert opened == [] and store.load() is None


@respx.mock
async def test_login_rejects_state_mismatch_without_calling_zoho(
    settings: Settings, store: TokenStore
) -> None:
    settings.ZOHO_REDIRECT_URI = "http://127.0.0.1:0/callback"  # port 0: the OS picks a free one
    token = respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "refresh_token": REFRESH})

    def evil_browser(url: str) -> None:
        threading.Thread(
            target=lambda: _hit(f"{_redirect_of(url)}?code=abc&state=forged"),
            daemon=True,
        ).start()

    with pytest.raises(AuthRequiredError, match="state mismatch"):
        await login(settings, store, open_browser=evil_browser, echo=lambda _: None, timeout=10)
    assert token.call_count == 0  # no code exchange, so the client secret never left the machine
    assert store.load() is None


@respx.mock
async def test_login_refuses_untrusted_accounts_server_before_sending_secret(
    settings: Settings, store: TokenStore
) -> None:
    settings.ZOHO_REDIRECT_URI = "http://127.0.0.1:0/callback"  # port 0: the OS picks a free one
    everything = respx.route().respond(200, json={})
    with pytest.raises(AuthRequiredError, match="untrusted"):
        await login(
            settings,
            store,
            open_browser=_browser("&accounts-server=https://accounts.zoho.evil.example"),
            echo=lambda _: None,
            timeout=10,
        )
    assert everything.call_count == 0
    assert store.load() is None


@respx.mock
async def test_login_follows_accounts_server_param_for_other_data_centre(
    settings: Settings, store: TokenStore
) -> None:
    settings.ZOHO_REDIRECT_URI = "http://127.0.0.1:0/callback"  # port 0: the OS picks a free one
    token = respx.post("https://accounts.zoho.eu/oauth/v2/token").respond(
        json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}
    )
    respx.get("https://www.zohoapis.eu/inventory/v1/organizations").respond(
        json={"organizations": [{"organization_id": 7, "name": "EU Co"}]}
    )
    stored = await login(
        settings,
        store,
        open_browser=_browser("&accounts-server=https://accounts.zoho.eu"),
        echo=lambda _: None,
        timeout=10,
    )
    assert token.call_count == 1
    assert stored.accounts_server == "https://accounts.zoho.eu"
    assert stored.api_domain == "https://www.zohoapis.eu"
    assert stored.org_id == "7"


@respx.mock
async def test_login_with_several_orgs_uses_the_chooser(
    settings: Settings, store: TokenStore
) -> None:
    settings.ZOHO_REDIRECT_URI = "http://127.0.0.1:0/callback"  # port 0: the OS picks a free one
    respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "refresh_token": REFRESH})
    respx.get(ORGS_URL).respond(
        json={
            "organizations": [
                {"organization_id": 1, "name": "First"},
                {"organization_id": 2, "name": "Second"},
            ]
        }
    )
    offered: list[list[Organization]] = []

    def choose(orgs: list[Organization]) -> Organization:
        offered.append(orgs)
        return orgs[1]

    stored = await login(
        settings,
        store,
        choose_org=choose,
        open_browser=_browser(),
        echo=lambda _: None,
        timeout=10,
    )
    assert len(offered[0]) == 2
    assert (stored.org_id, stored.org_name) == ("2", "Second")


# ---------------------------------------------------------------- the refresh request


@respx.mock
async def test_refresh_request_body_and_endpoint(settings: Settings, store: TokenStore) -> None:
    store.save(_stored().model_copy(update={"accounts_server": "https://accounts.zoho.com"}))
    route = respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "expires_in": 3600})
    await TokenManager(settings, store).get_access_token()
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["client_id"] == ["cid"]
    assert form["client_secret"] == [SECRET]
    assert form["refresh_token"] == [REFRESH]
    assert SECRET not in str(route.calls.last.request.url)  # never in the query string


@respx.mock
async def test_refresh_uses_the_stored_data_centre(settings: Settings, store: TokenStore) -> None:
    store.save(_stored().model_copy(update={"accounts_server": "https://accounts.zoho.eu"}))
    route = respx.post("https://accounts.zoho.eu/oauth/v2/token").respond(
        json={"access_token": ACCESS, "expires_in": 3600}
    )
    await TokenManager(settings, store).get_access_token()
    assert route.call_count == 1


async def test_refresh_when_not_logged_in(settings: Settings, store: TokenStore) -> None:
    with pytest.raises(AuthRequiredError, match="Not logged in"):
        await TokenManager(settings, store).get_access_token()


@respx.mock
async def test_refresh_network_error_names_no_secret(settings: Settings, store: TokenStore) -> None:
    store.save(_stored())
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError(f"boom {SECRET} {REFRESH}"))
    with pytest.raises(AuthRequiredError) as err:
        await TokenManager(settings, store).get_access_token()
    assert SECRET not in err.value.message and REFRESH not in err.value.message


@respx.mock
async def test_refresh_response_without_access_token(settings: Settings, store: TokenStore) -> None:
    store.save(_stored())
    respx.post(TOKEN_URL).respond(json={"expires_in": 3600})
    with pytest.raises(AuthRequiredError, match="no access_token"):
        await TokenManager(settings, store).get_access_token()


# ---------------------------------------------------------------- invalidate semantics


@respx.mock
async def test_invalidate_with_stale_token_keeps_the_fresh_one(
    settings: Settings, store: TokenStore
) -> None:
    store.save(_stored())
    route = respx.post(TOKEN_URL).respond(json={"access_token": ACCESS, "expires_in": 3600})
    mgr = TokenManager(settings, store)
    await mgr.get_access_token()
    mgr.invalidate("1000.some-older-token-that-was-rejected")
    await mgr.get_access_token()
    assert route.call_count == 1
    mgr.invalidate(ACCESS)  # the rejected token is the cached one: now it goes
    await mgr.get_access_token()
    assert route.call_count == 2


# ---------------------------------------------------------------- token store failure paths


def test_store_corrupt_payload(settings: Settings, store: TokenStore) -> None:
    key = settings.TOKEN_ENCRYPTION_KEY.get_secret_value().encode()
    store.path.write_bytes(Fernet(key).encrypt(b"{not json}"))
    with pytest.raises(AuthRequiredError, match="corrupt"):
        store.load()


def test_store_save_leaves_no_temp_files_and_survives_overwrite(store: TokenStore) -> None:
    store.save(_stored())
    store.save(_stored().model_copy(update={"org_id": "43"}))
    loaded = store.load()
    assert loaded is not None and loaded.org_id == "43"
    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]


def test_store_plaintext_file_is_not_readable(store: TokenStore) -> None:
    store.path.write_text('{"refresh_token": "plain"}')
    with pytest.raises(AuthRequiredError):
        store.load()


# ---------------------------------------------------------------- revoke


@respx.mock
async def test_revoke_success_and_failure() -> None:
    ok = respx.post("https://accounts.zoho.com/oauth/v2/token/revoke").respond(
        json={"status": "success"}
    )
    await revoke_refresh_token(_stored())
    assert ok.call_count == 1

    ok.respond(json={"status": "failure"})
    with pytest.raises(AuthRequiredError, match="refused to revoke"):
        await revoke_refresh_token(_stored())
