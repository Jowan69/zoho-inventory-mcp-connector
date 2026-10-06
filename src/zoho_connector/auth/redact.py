"""Logging filter that hides Zoho tokens and the client secret."""

import logging
import re

REDACTED = "[REDACTED]"
# Zoho access and refresh tokens look like "1000.<hex>.<hex>".
_TOKEN_RE = re.compile(r"1000\.[A-Za-z0-9._-]+")
# Zoho's revoke endpoint takes the token as a query parameter, whatever the token looks like.
_TOKEN_PARAM_RE = re.compile(r"([?&]token=)[^&\s\"']+")


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _TOKEN_PARAM_RE.sub(rf"\g<1>{REDACTED}", text)
    return _TOKEN_RE.sub(REDACTED, text)


class RedactingFilter(logging.Filter):
    """Rewrites each record so tokens and secrets never reach a handler."""

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = None
        if record.exc_info:
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted, self._secrets)
            record.exc_info = None
        return True


def install_redaction(*secrets: str) -> RedactingFilter:
    """Attach the filter to every handler on the root logger."""
    flt = RedactingFilter(*secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(flt)
    return flt


def protect_httpx_logging() -> None:
    """Scrub credentials from httpx's own request log line, whatever the logging setup.

    httpx logs every request URL at INFO on the "httpx" logger. A filter on that logger runs
    before any handler or propagation, so it holds even if nobody called install_redaction.
    Safe to call more than once.
    """
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
