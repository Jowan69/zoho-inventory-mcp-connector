"""PII masking for customer contact details returned to the agent."""

import re

_NON_DIGITS = re.compile(r"\D")


def mask_email(email: str | None) -> str | None:
    """jo@example.com -> j***@example.com. Anything that is not an address becomes '***'."""
    if not email:
        return None
    local, sep, domain = email.strip().rpartition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def mask_phone(phone: str | None) -> str | None:
    """+1 (555) 010-1234 -> ***1234. Only the last 4 digits survive."""
    if not phone:
        return None
    digits = _NON_DIGITS.sub("", phone)
    if not digits:
        return None
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"
