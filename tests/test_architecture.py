"""Guards for the hard rules in CLAUDE.md, checked by reading the source, not by running it."""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "zoho_connector"
MUTATING = {"post", "put", "patch", "delete"}
RAW_REQUEST = {"request", "send", "stream"}


def _modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in SRC.rglob("*.py")
    }


def _imports_httpx(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "httpx" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "httpx":
            return True
    return False


def _method_calls(tree: ast.Module, names: set[str]) -> set[str]:
    return {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in names
    }


def _http_calls(tree: ast.Module) -> set[str]:
    """Method names called on an httpx client: `http.<m>(...)` or `self._http.<m>(...)`."""
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            receiver = n.func.value
            name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
            if name in {"http", "_http"}:
                found.add(n.func.attr)
    return found


def test_only_known_modules_import_httpx() -> None:
    importers = {name for name, tree in _modules().items() if _imports_httpx(tree)}
    # tools/, server.py, cli.py, config.py and models.py must reach Zoho through ZohoClient.
    assert importers == {
        "auth/oauth.py",  # accounts.zoho.* token endpoints, plus the one organizations lookup
        "client/cache.py",  # only QueryParams for key building
        "client/demo.py",  # in-process transport
        "client/zoho_client.py",
    }


def test_tools_never_build_their_own_http_client() -> None:
    for name, tree in _modules().items():
        if name.startswith("tools/") or name in {"server.py", "cli.py"}:
            assert not _imports_httpx(tree), name
            assert not _method_calls(tree, MUTATING | RAW_REQUEST), name


def test_zoho_client_issues_only_get_requests() -> None:
    tree = _modules()["client/zoho_client.py"]
    assert _http_calls(tree) == {"get", "aclose"}


def test_demo_transport_never_opens_a_connection() -> None:
    tree = _modules()["client/demo.py"]
    assert _http_calls(tree) == set()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "AsyncHTTPTransport" not in names and "AsyncClient" not in names


def test_only_the_oauth_module_posts() -> None:
    """POSTs go to accounts.zoho.* (token, revoke); nothing posts to the Inventory API."""
    posters = {name for name, tree in _modules().items() if _http_calls(tree) & MUTATING}
    assert posters == {"auth/oauth.py"}


def test_settings_never_reveal_secrets_in_repr_or_dump() -> None:
    """Secrets are SecretStr in Settings, so repr/str of settings never shows them."""
    from pydantic import SecretStr

    from zoho_connector.config import Settings

    s = Settings(
        _env_file=None,
        ZOHO_CLIENT_SECRET=SecretStr("sh-client-secret"),
        TOKEN_ENCRYPTION_KEY=SecretStr("sh-key"),
        MCP_SERVER_TOKEN=SecretStr("sh-bearer"),
    )
    text = repr(s) + str(s) + s.model_dump_json()
    for secret in ("sh-client-secret", "sh-key", "sh-bearer"):
        assert secret not in text
