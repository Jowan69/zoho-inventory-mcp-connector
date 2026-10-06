"""Connector error hierarchy; each error carries a code returned to the agent."""


class ConnectorError(Exception):
    """Base class for all connector errors."""

    code: str = "UPSTREAM_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class NotFoundError(ConnectorError):
    code = "NOT_FOUND"


class InvalidInputError(ConnectorError):
    code = "INVALID_INPUT"


class RateLimitedError(ConnectorError):
    code = "RATE_LIMITED"


class DailyQuotaExhaustedError(ConnectorError):
    code = "DAILY_QUOTA_EXHAUSTED"


class AuthRequiredError(ConnectorError):
    code = "AUTH_REQUIRED"


class UpstreamError(ConnectorError):
    code = "UPSTREAM_ERROR"
