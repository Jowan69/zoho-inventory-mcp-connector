"""Typer command line interface (auth, serve, demo seeding)."""

import asyncio
import logging

import typer

from zoho_connector.auth.oauth import TokenManager, login, revoke_refresh_token
from zoho_connector.auth.redact import install_redaction
from zoho_connector.auth.token_store import TokenStore
from zoho_connector.config import Settings
from zoho_connector.errors import ConnectorError

app = typer.Typer(help="Zoho Inventory read-only MCP connector.", no_args_is_help=True)
auth_app = typer.Typer(help="Zoho OAuth login, status and logout.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")


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
