"""Zoho OAuth 2.0 authorization-code flow (offline access) and access-token management."""

import asyncio
import hmac
import re
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from zoho_connector.auth.token_store import StoredTokens, TokenStore
from zoho_connector.config import Settings
from zoho_connector.errors import AuthRequiredError, UpstreamError

SCOPES = "ZohoInventory.items.READ,ZohoInventory.salesorders.READ,ZohoInventory.settings.READ"
LOGIN_TIMEOUT_S = 180
REFRESH_MARGIN_S = 300
HTTP_TIMEOUT_S = 30.0

# The accounts-server value arrives in a browser redirect, so only known Zoho hosts are trusted
# before the client secret is sent to it.
_ACCOUNTS_RE = re.compile(r"^https://accounts\.zoho\.(com|eu|in|com\.au|jp|ca|sa|com\.cn)$")

_SUCCESS_HTML = (
    "<!doctype html><html><body><h3>Login complete. You can close this tab.</h3></body></html>"
)
_FAILURE_HTML = "<!doctype html><html><body><h3>Login failed. See the terminal.</h3></body></html>"


@dataclass(frozen=True)
class Organization:
    org_id: str
    name: str


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int


def derive_api_domain(accounts_server: str) -> str:
    """https://accounts.zoho.X -> https://www.zohoapis.X (rejects non-Zoho hosts)."""
    accounts_server = accounts_server.rstrip("/")
    match = _ACCOUNTS_RE.match(accounts_server)
    if not match:
        raise AuthRequiredError(f"Refusing untrusted Zoho accounts server: {accounts_server!r}")
    return f"https://www.zohoapis.{match.group(1)}"


def build_auth_url(settings: Settings, state: str) -> str:
    query = urlencode(
        {
            "scope": SCOPES,
            "client_id": settings.ZOHO_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.ZOHO_REDIRECT_URI,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{settings.accounts_base}/oauth/v2/auth?{query}"


def _require_credentials(settings: Settings) -> None:
    if not settings.ZOHO_CLIENT_ID or not settings.ZOHO_CLIENT_SECRET.get_secret_value():
        raise AuthRequiredError("ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET must be set.")


# --------------------------------------------------------------------------- callback server


def check_callback(params: dict[str, str], expected_state: str) -> dict[str, str]:
    """Validate callback query parameters; raise AuthRequiredError if the login must be rejected."""
    if not hmac.compare_digest(params.get("state", "").encode(), expected_state.encode()):
        raise AuthRequiredError("OAuth state mismatch; login rejected.")
    if "error" in params:
        raise AuthRequiredError(f"Zoho returned an authorization error: {params['error']}")
    if not params.get("code"):
        raise AuthRequiredError("Callback did not contain an authorization code.")
    return params


def wait_for_callback(
    redirect_uri: str, expected_state: str, timeout: float = LOGIN_TIMEOUT_S
) -> dict[str, str]:
    """Run a one-shot HTTP server on the redirect URI's host/port until the callback arrives."""
    parsed = urlparse(redirect_uri)
    host, port, path = parsed.hostname or "localhost", parsed.port or 80, parsed.path or "/"
    outcome: dict[str, dict[str, str] | AuthRequiredError] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib hook name)
            url = urlparse(self.path)
            if url.path != path:
                self.send_error(404)
                return
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                outcome["ok"] = check_callback(params, expected_state)
                status, body = 200, _SUCCESS_HTML
            except AuthRequiredError as exc:
                outcome["err"] = exc
                status, body = 400, _FAILURE_HTML
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # the request line holds the one-time code; keep it out of stderr

    try:
        server = HTTPServer((host, port), Handler)
    except OSError as exc:
        raise AuthRequiredError(
            f"Cannot listen on {host}:{port} for the OAuth callback: {exc}"
        ) from exc
    server.timeout = 0.5
    deadline = time.monotonic() + timeout
    try:
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "err" in outcome:
        raise outcome["err"]  # type: ignore[misc]
    if "ok" not in outcome:
        raise AuthRequiredError(f"Timed out after {timeout:.0f} s waiting for the Zoho callback.")
    return outcome["ok"]  # type: ignore[return-value]


# --------------------------------------------------------------------------- token endpoint


def _parse_token_response(resp: httpx.Response, what: str) -> dict[str, object]:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not isinstance(body, dict) or "error" in body:
        reason = body.get("error") if isinstance(body, dict) else None
        raise AuthRequiredError(
            f"Zoho rejected the {what} (HTTP {resp.status_code}, error={reason or 'unknown'})."
        )
    return body


async def exchange_code(
    http: httpx.AsyncClient, settings: Settings, accounts_server: str, code: str
) -> TokenResponse:
    resp = await http.post(
        f"{accounts_server}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": settings.ZOHO_CLIENT_ID,
            "client_secret": settings.ZOHO_CLIENT_SECRET.get_secret_value(),
            "redirect_uri": settings.ZOHO_REDIRECT_URI,
            "code": code,
        },
    )
    body = _parse_token_response(resp, "authorization code")
    access = body.get("access_token")
    if not isinstance(access, str):
        raise AuthRequiredError("Zoho token response had no access_token.")
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        dc = accounts_server.rsplit("accounts.zoho.", 1)[-1]
        raise AuthRequiredError(
            "Zoho did not return a refresh_token. Revoke the old grant at "
            f"https://accounts.zoho.{dc} > Connected Apps, then run `auth login` again."
        )
    expires = body.get("expires_in")
    return TokenResponse(access, refresh, int(expires) if isinstance(expires, int) else 3600)


async def fetch_organizations(
    http: httpx.AsyncClient, api_domain: str, access_token: str
) -> list[Organization]:
    # The one Zoho call without organization_id: it is how the id is discovered.
    resp = await http.get(
        f"{api_domain}/inventory/v1/organizations",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
    )
    if resp.status_code != 200:
        raise UpstreamError(f"Listing organizations failed (HTTP {resp.status_code}).")
    orgs = resp.json().get("organizations") or []
    result = [Organization(str(o["organization_id"]), str(o["name"])) for o in orgs]
    if not result:
        raise UpstreamError("This Zoho account has no Inventory organizations.")
    return result


def prompt_for_org(orgs: list[Organization]) -> Organization:
    print("Several organizations found:")
    for i, org in enumerate(orgs, 1):
        print(f"  {i}. {org.name} (id {org.org_id})")
    while True:
        answer = input(f"Pick one [1-{len(orgs)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(orgs):
            return orgs[int(answer) - 1]
        print("Invalid choice.")


# --------------------------------------------------------------------------- login


async def login(
    settings: Settings,
    store: TokenStore,
    *,
    choose_org: Callable[[list[Organization]], Organization] = prompt_for_org,
    open_browser: Callable[[str], object] = webbrowser.open,
    echo: Callable[[str], object] = print,
    timeout: float = LOGIN_TIMEOUT_S,
) -> StoredTokens:
    """Run the full browser login and persist the refresh token and chosen organization."""
    _require_credentials(settings)
    state = secrets.token_urlsafe(32)
    url = build_auth_url(settings, state)
    echo(f"Open this URL to authorize (opening your browser now):\n{url}")
    open_browser(url)

    params = await asyncio.to_thread(wait_for_callback, settings.ZOHO_REDIRECT_URI, state, timeout)

    accounts_server = settings.accounts_base
    api_domain = settings.api_base.removesuffix("/inventory/v1")
    if "accounts-server" in params:
        accounts_server = params["accounts-server"].rstrip("/")
        api_domain = derive_api_domain(accounts_server)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
        # The grant code lives 60 s, so exchange it before anything else.
        tokens = await exchange_code(http, settings, accounts_server, params["code"])
        assert tokens.refresh_token is not None  # exchange_code guarantees it
        orgs = await fetch_organizations(http, api_domain, tokens.access_token)

    org = orgs[0] if len(orgs) == 1 else choose_org(orgs)
    stored = StoredTokens(
        refresh_token=tokens.refresh_token,
        accounts_server=accounts_server,
        api_domain=api_domain,
        org_id=org.org_id,
        org_name=org.name,
    )
    store.save(stored)
    return stored


# --------------------------------------------------------------------------- token manager


class TokenManager:
    """Hands out access tokens; concurrent callers trigger at most one refresh."""

    def __init__(
        self,
        settings: Settings,
        store: TokenStore,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._store = store
        self._http = http
        self._clock = clock
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._expires_at = 0.0

    def seconds_left(self) -> float:
        """Seconds until the cached access token expires (0 if none is cached)."""
        return max(0.0, self._expires_at - self._clock()) if self._access_token else 0.0

    def invalidate(self) -> None:
        """Drop the cached access token (call on HTTP 401)."""
        self._access_token = None
        self._expires_at = 0.0

    async def get_access_token(self) -> str:
        async with self._lock:
            if self._access_token and self.seconds_left() > REFRESH_MARGIN_S:
                return self._access_token
            await self._refresh()
            assert self._access_token is not None
            return self._access_token

    async def _refresh(self) -> None:
        _require_credentials(self._settings)
        stored = self._store.load()
        if stored is None:
            raise AuthRequiredError("Not logged in. Run `zoho-connector auth login`.")
        data = {
            "grant_type": "refresh_token",
            "client_id": self._settings.ZOHO_CLIENT_ID,
            "client_secret": self._settings.ZOHO_CLIENT_SECRET.get_secret_value(),
            "refresh_token": stored.refresh_token,
        }
        url = f"{stored.accounts_server}/oauth/v2/token"
        try:
            if self._http is not None:
                resp = await self._http.post(url, data=data)
            else:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
                    resp = await http.post(url, data=data)
        except httpx.HTTPError as exc:
            raise AuthRequiredError(f"Token refresh failed: {type(exc).__name__}.") from exc
        body = _parse_token_response(resp, "refresh token")
        access = body.get("access_token")
        if not isinstance(access, str):
            raise AuthRequiredError("Zoho refresh response had no access_token.")
        expires = body.get("expires_in")
        self._access_token = access
        self._expires_at = self._clock() + (expires if isinstance(expires, int) else 3600)


async def revoke_refresh_token(stored: StoredTokens) -> None:
    """Revoke the refresh token at Zoho; raises AuthRequiredError if Zoho refuses."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
        try:
            resp = await http.post(
                f"{stored.accounts_server}/oauth/v2/token/revoke",
                params={"token": stored.refresh_token},
            )
        except httpx.HTTPError as exc:
            raise AuthRequiredError(f"Revoke request failed: {type(exc).__name__}.") from exc
    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    if resp.status_code != 200 or (isinstance(body, dict) and body.get("status") == "failure"):
        raise AuthRequiredError(f"Zoho refused to revoke the token (HTTP {resp.status_code}).")
