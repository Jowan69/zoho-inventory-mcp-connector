from zoho_connector import errors
from zoho_connector.config import Settings


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.ZOHO_DC == "com"
    assert s.DAILY_BUDGET == 900
    assert s.api_base == "https://www.zohoapis.com/inventory/v1"
    assert s.accounts_base == "https://accounts.zoho.com"


def test_error_codes() -> None:
    codes = {
        errors.NotFoundError: "NOT_FOUND",
        errors.InvalidInputError: "INVALID_INPUT",
        errors.RateLimitedError: "RATE_LIMITED",
        errors.DailyQuotaExhaustedError: "DAILY_QUOTA_EXHAUSTED",
        errors.AuthRequiredError: "AUTH_REQUIRED",
        errors.UpstreamError: "UPSTREAM_ERROR",
    }
    for cls, code in codes.items():
        assert issubclass(cls, errors.ConnectorError)
        assert cls().code == code
