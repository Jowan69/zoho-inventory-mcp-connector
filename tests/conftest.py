"""Shared fixtures: keep every test independent of the developer's or CI's environment."""

import pytest

from zoho_connector.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every env var Settings reads (e.g. ZOHO_DEMO=1 in CI); tests pass what they need."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
