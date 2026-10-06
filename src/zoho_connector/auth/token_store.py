"""Fernet-encrypted storage for the long-lived Zoho refresh token."""

import contextlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, SecretStr, ValidationError

from zoho_connector.errors import AuthRequiredError

DEFAULT_TOKEN_PATH = Path("tokens") / "zoho_tokens.enc"


class StoredTokens(BaseModel):
    """What we persist after a successful login."""

    refresh_token: str
    accounts_server: str
    api_domain: str
    org_id: str
    org_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TokenStore:
    """Reads and writes ``StoredTokens`` as one encrypted JSON file."""

    def __init__(self, key: SecretStr | str, path: Path = DEFAULT_TOKEN_PATH) -> None:
        raw = key.get_secret_value() if isinstance(key, SecretStr) else key
        if not raw:
            raise AuthRequiredError(
                "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        try:
            self._fernet = Fernet(raw.encode())
        except ValueError as exc:
            raise AuthRequiredError(
                "TOKEN_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)."
            ) from exc
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> StoredTokens | None:
        """Return the stored tokens, or None if nothing has been saved yet."""
        if not self.exists():
            return None
        try:
            plain = self._fernet.decrypt(self.path.read_bytes())
            return StoredTokens.model_validate_json(plain)
        except InvalidToken as exc:
            raise AuthRequiredError(
                f"Cannot decrypt {self.path}: TOKEN_ENCRYPTION_KEY does not match the key "
                "used to save it. Restore the old key, or run `auth logout` and `auth login`."
            ) from exc
        except ValidationError as exc:
            raise AuthRequiredError(
                f"{self.path} is corrupt. Run `auth logout` and `auth login` again."
            ) from exc

    def save(self, tokens: StoredTokens) -> None:
        """Atomically write the tokens (temp file in the same directory, then replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._fernet.encrypt(tokens.model_dump_json().encode())
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".zoho_tokens.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
