"""Typer command line interface (auth, serve, demo seeding)."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import typer
import uvicorn
from pydantic import BaseModel

from zoho_connector import tools
from zoho_connector.auth.oauth import TokenManager, login, revoke_refresh_token
from zoho_connector.auth.redact import install_redaction
from zoho_connector.auth.token_store import TokenStore
from zoho_connector.client.demo import build_demo_client
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import ConnectorError
from zoho_connector.server import create_http_app, create_server, export_tool_definitions

app = typer.Typer(help="Zoho Inventory read-only MCP connector.", no_args_is_help=True)
auth_app = typer.Typer(help="Zoho OAuth login, status and logout.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
debug_app = typer.Typer(help="Debug helpers that talk to Zoho.", no_args_is_help=True)
app.add_typer(debug_app, name="debug")
tools_app = typer.Typer(
    help="Run the read-only tools and print JSON (set ZOHO_DEMO=1 for fixture data).",
    no_args_is_help=True,
)
app.add_typer(tools_app, name="tools")


@app.callback()
def _main() -> None:
    logging.basicConfig(level=logging.WARNING)
    install_redaction(Settings().ZOHO_CLIENT_SECRET.get_secret_value())


def _fail(exc: ConnectorError) -> typer.Exit:
    typer.echo(f"[{exc.code}] {exc.message}", err=True)
    return typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print the connector version (placeholder)."""
    typer.echo("zoho-connector 0.1.0")


@auth_app.command("login")
def auth_login() -> None:
    """Authorize in the browser and store the encrypted refresh token."""
    settings = Settings()
    try:
        store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
        stored = asyncio.run(login(settings, store, echo=typer.echo))
    except ConnectorError as exc:
        raise _fail(exc) from exc
    typer.echo(f"Logged in. Organization: {stored.org_name} ({stored.org_id}).")


@auth_app.command("status")
def auth_status() -> None:
    """Show login state; checks the refresh token by requesting one access token."""
    settings = Settings()
    try:
        store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
        stored = store.load()
    except ConnectorError as exc:
        raise _fail(exc) from exc
    if stored is None:
        typer.echo("Not logged in. Run `auth login`.")
        raise typer.Exit(code=1)

    manager = TokenManager(settings, store)
    valid, minutes = False, 0.0
    try:
        asyncio.run(manager.get_access_token())
        valid, minutes = True, manager.seconds_left() / 60
    except ConnectorError as exc:
        typer.echo(f"Refresh failed: [{exc.code}] {exc.message}", err=True)

    dc = stored.accounts_server.rsplit("accounts.zoho.", 1)[-1]
    typer.echo(f"Data center:         {dc}")
    typer.echo(f"Organization:        {stored.org_name} ({stored.org_id})")
    typer.echo("Refresh token:       present")
    typer.echo(f"Access token valid:  {'yes' if valid else 'no'}")
    typer.echo(f"Minutes left:        {minutes:.0f}")
    if not valid:
        raise typer.Exit(code=1)


@auth_app.command("logout")
def auth_logout(
    force: bool = typer.Option(False, "--force", help="Clear local tokens even if revoke fails."),
) -> None:
    """Revoke the refresh token at Zoho, then delete the local store."""
    settings = Settings()
    try:
        store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
        stored = None
        try:
            stored = store.load()
        except ConnectorError as exc:  # e.g. wrong key: nothing to revoke with
            if not force:
                raise _fail(exc) from exc
        if stored is not None:
            try:
                asyncio.run(revoke_refresh_token(stored))
                typer.echo("Refresh token revoked at Zoho.")
            except ConnectorError as exc:
                typer.echo(f"[{exc.code}] {exc.message}", err=True)
                if not force:
                    typer.echo("Local tokens kept. Use --force to clear them anyway.", err=True)
                    raise typer.Exit(code=1) from exc
    except ConnectorError as exc:
        raise _fail(exc) from exc
    store.clear()
    typer.echo("Local token store cleared.")


def _build_client(settings: Settings) -> ZohoClient:
    store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
    return ZohoClient(settings, store, TokenManager(settings, store))


@debug_app.command("ping")
def debug_ping() -> None:
    """GET /organizations and print the organization name and status (uses 1 request)."""
    settings = Settings()

    async def run() -> tuple[str, dict[str, object]]:
        store = TokenStore(settings.TOKEN_ENCRYPTION_KEY)
        stored = store.load()
        async with ZohoClient(settings, store, TokenManager(settings, store)) as client:
            body = await client.get("/organizations", ttl=0)
            orgs = body.get("organizations")
            names = {
                str(o.get("organization_id")): str(o.get("name"))
                for o in (orgs if isinstance(orgs, list) else [])
                if isinstance(o, dict)
            }
            org_id = settings.ZOHO_ORG_ID or (stored.org_id if stored else "")
            return names.get(org_id, "unknown"), client.quota()

    try:
        org_name, quota = asyncio.run(run())
    except ConnectorError as exc:
        raise _fail(exc) from exc
    typer.echo(f"Organization: {org_name}")
    typer.echo("Status:       OK")
    typer.echo(f"Used today:   {quota['used_today']} of {quota['budget']}")


@debug_app.command("quota")
def debug_quota() -> None:
    """Show today's request count against DAILY_BUDGET (no Zoho call)."""
    settings = Settings()

    async def run() -> dict[str, object]:
        async with _build_client(settings) as client:
            return client.quota()

    try:
        quota = asyncio.run(run())
    except ConnectorError as exc:
        raise _fail(exc) from exc
    typer.echo(f"Used today: {quota['used_today']}")
    typer.echo(f"Budget:     {quota['budget']}")
    typer.echo(f"Remaining:  {quota['remaining']}")
    typer.echo(f"Warning:    {'yes' if quota['warning'] else 'no'}")


def _run_tool(call: Callable[[ZohoClient, Settings], Awaitable[BaseModel]]) -> None:
    """Run one tool against the live client (or the demo client) and print its JSON."""
    settings = Settings()

    async def run() -> BaseModel:
        client = build_demo_client(settings) if settings.ZOHO_DEMO else _build_client(settings)
        async with client:
            return await call(client, settings)

    try:
        result = asyncio.run(run())
    except ConnectorError as exc:
        raise _fail(exc) from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@tools_app.command("list-orders")
def tools_list_orders(
    status: str | None = typer.Option(None, help="Order status, e.g. confirmed or void."),
    date_from: str | None = typer.Option(None, help="YYYY-MM-DD, inclusive."),
    date_to: str | None = typer.Option(None, help="YYYY-MM-DD, inclusive."),
    page: int = typer.Option(1),
    per_page: int = typer.Option(25),
) -> None:
    """List sales orders."""
    _run_tool(lambda c, _s: tools.list_sales_orders(c, status, date_from, date_to, page, per_page))


@tools_app.command("get-order")
def tools_get_order(salesorder_id: str) -> None:
    """Show one sales order with line items (contact details masked)."""
    _run_tool(lambda c, _s: tools.get_sales_order(c, salesorder_id))


@tools_app.command("search-orders")
def tools_search_orders(
    query: str = typer.Argument(..., help="Order number, reference or customer name."),
    status: str | None = typer.Option(None),
    page: int = typer.Option(1),
    per_page: int = typer.Option(25),
) -> None:
    """Search sales orders."""
    _run_tool(lambda c, _s: tools.search_sales_orders(c, query, status, page, per_page))


@tools_app.command("list-items")
def tools_list_items(
    status: str = typer.Option("active", help="active, inactive, all or lowstock."),
    page: int = typer.Option(1),
    per_page: int = typer.Option(25),
) -> None:
    """List items."""
    _run_tool(lambda c, _s: tools.list_items(c, status, page, per_page))


@tools_app.command("get-item")
def tools_get_item(item_id: str) -> None:
    """Show one item with stock per location."""
    _run_tool(lambda c, _s: tools.get_item(c, item_id))


@tools_app.command("search-items")
def tools_search_items(
    query: str = typer.Argument(..., help="Item name or SKU."),
    page: int = typer.Option(1),
    per_page: int = typer.Option(25),
) -> None:
    """Search items."""
    _run_tool(lambda c, _s: tools.search_items(c, query, page, per_page))


@tools_app.command("status")
def tools_status() -> None:
    """Show mode, login state and today's request usage (no Zoho call)."""
    _run_tool(tools.connector_status)


@app.command()
def serve(
    transport: str = typer.Option("stdio", help="stdio or http (Streamable HTTP at /mcp)."),
    host: str = typer.Option("127.0.0.1", help="Bind address for --transport http."),
    port: int = typer.Option(8000, help="Port for --transport http."),
) -> None:
    """Run the MCP server. Logs go to stderr; on stdio, stdout is the protocol channel."""
    if transport not in ("stdio", "http"):
        typer.echo("--transport must be stdio or http.", err=True)
        raise typer.Exit(code=2)
    settings = Settings()
    if transport == "stdio":
        create_server(settings).run("stdio")
        return
    if host != "127.0.0.1" and not settings.MCP_SERVER_TOKEN.get_secret_value():
        typer.echo(
            f"WARNING: listening on {host} with no MCP_SERVER_TOKEN set; "
            "anyone who can reach this port can read your Zoho data.",
            err=True,
        )
    uvicorn.run(create_http_app(settings, host), host=host, port=port, log_level="info")


@app.command("export-tools")
def export_tools(
    output: str = typer.Option("tools.json", help="File to write, relative to the cwd."),
) -> None:
    """Write tools.json (name, title, description, inputSchema, annotations) from the server."""
    definitions = asyncio.run(export_tool_definitions(Settings()))
    Path(output).write_text(json.dumps(definitions, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Wrote {len(definitions)} tools to {output}.", err=True)
