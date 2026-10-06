"""connector_status: local state only, never calls Zoho and never costs a request."""

from zoho_connector.auth.token_store import TokenStore
from zoho_connector.client.demo import DEMO_ORG_NAME
from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.config import Settings
from zoho_connector.errors import ConnectorError
from zoho_connector.models import ConnectorStatus


def _login_state(settings: Settings) -> tuple[bool, str | None]:
    try:
        stored = TokenStore(settings.TOKEN_ENCRYPTION_KEY).load()
    except ConnectorError:  # no key, or a key that no longer matches the store
        return False, None
    return (stored is not None), (stored.org_name if stored else None)


async def connector_status(client: ZohoClient, settings: Settings) -> ConnectorStatus:
    quota = client.quota()
    if settings.ZOHO_DEMO:
        authenticated, org_name = False, DEMO_ORG_NAME
    else:
        authenticated, org_name = _login_state(settings)
    return ConnectorStatus(
        mode="demo" if settings.ZOHO_DEMO else "live",
        authenticated=authenticated,
        org_name=org_name,
        used_today=quota["used_today"],
        budget=quota["budget"],
    )
