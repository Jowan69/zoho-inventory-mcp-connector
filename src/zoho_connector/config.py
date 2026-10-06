"""Application settings loaded from environment variables and .env."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Connector configuration."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: SecretStr = SecretStr("")
    ZOHO_DC: str = "com"
    ZOHO_REDIRECT_URI: str = "http://localhost:8765/callback"
    TOKEN_ENCRYPTION_KEY: SecretStr = SecretStr("")
    ZOHO_ORG_ID: str | None = None
    ZOHO_DEMO: bool = False
    DAILY_BUDGET: int = 900
    MCP_SERVER_TOKEN: SecretStr = SecretStr("")

    @property
    def api_base(self) -> str:
        return f"https://www.zohoapis.{self.ZOHO_DC}/inventory/v1"

    @property
    def accounts_base(self) -> str:
        return f"https://accounts.zoho.{self.ZOHO_DC}"
