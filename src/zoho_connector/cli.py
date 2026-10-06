"""Typer command line interface (auth, serve, demo seeding)."""

import typer

app = typer.Typer(help="Zoho Inventory read-only MCP connector.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the connector version (placeholder)."""
    typer.echo("zoho-connector 0.1.0")
