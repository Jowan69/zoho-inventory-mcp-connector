import pytest

from zoho_connector.tools.masking import mask_email, mask_phone


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("jo@example.com", "j***@example.com"),
        ("Jo.Smith+x@sub.example.co.uk", "J***@sub.example.co.uk"),
        ("not-an-email", "***"),
        ("@example.com", "***"),
        ("", None),
        (None, None),
    ],
)
def test_mask_email(raw: str | None, masked: str | None) -> None:
    assert mask_email(raw) == masked


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("+1 (555) 010-1234", "***1234"),
        ("9876543210", "***3210"),
        ("123", "***"),
        ("n/a", None),
        ("", None),
        (None, None),
    ],
)
def test_mask_phone(raw: str | None, masked: str | None) -> None:
    assert mask_phone(raw) == masked
